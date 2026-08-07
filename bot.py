"""
TransTele — English -> Persian word-by-word translator bot.

Send a list of English words (one per line) in a DM, a group, or a channel where
the bot is an admin. The bot replies with each word followed by its Persian
meaning, separated by a dashed line.

Translation is done with `deep-translator`, which talks to the free public
endpoints of Google Translate / MyMemory. No API key, no billing account.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from functools import lru_cache

from deep_translator import GoogleTranslator, MyMemoryTranslator
from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Read the token from the environment. Optionally loaded from a .env file below.
try:  # python-dotenv is optional
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# How the bot should answer a channel post:
#   "reply" -> post the translation as a separate message replying to the post
#   "edit"  -> rewrite the original post so it contains word + translation
CHANNEL_MODE = os.getenv("CHANNEL_MODE", "reply").strip().lower()

SEPARATOR = "----------------------------------------------------------"

# Telegram hard limit is 4096 characters per message.
TELEGRAM_MAX_CHARS = 4000

# Words handled at the same time. Keep it modest: the free endpoints throttle
# aggressive clients.
MAX_CONCURRENCY = 5

# Refuse absurdly long lists so one message cannot pin the bot for minutes.
MAX_WORDS_PER_MESSAGE = 120

# MULTI_SENSE=1 prints several meanings per word ("خمیازه، دهن‌دره") instead of
# just the best one. Off by default.
MULTI_SENSE = os.getenv("MULTI_SENSE", "0").strip() in {"1", "true", "yes", "on"}
MAX_SENSES = 3

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("transtele")

# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #

# Strips list decorations: "1. word", "1) word", "- word", "* word", "• word"
_BULLET_RE = re.compile(r"^\s*(?:\d+\s*[.)\]-]|[-*•–—])\s*")
_PERSIAN_RE = re.compile(r"[؀-ۿ]")


def extract_words(text: str) -> list[str]:
    """Turn a raw message into the list of terms to translate.

    One term per line. A line containing commas is split further, so both
    "yawn\nbare" and "yawn, bare" work. Order is preserved, duplicates removed.
    """
    words: list[str] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = _BULLET_RE.sub("", raw_line).strip()
        if not line:
            continue

        parts = [p.strip() for p in re.split(r"[,،;]", line)] if re.search(r"[,،;]", line) else [line]

        for part in parts:
            part = part.strip(" \t\"'.:!?")
            if not part:
                continue
            key = part.casefold()
            if key in seen:
                continue
            seen.add(key)
            words.append(part)

    return words


def looks_like_output(text: str) -> bool:
    """True if the text is (probably) a translation the bot itself produced.

    Needed for channels: every message the bot posts to a channel comes back as
    a new `channel_post` update, which would otherwise trigger an endless loop.
    """
    if SEPARATOR[:20] in text:
        return True
    persian_chars = len(_PERSIAN_RE.findall(text))
    return persian_chars > len(text.replace(" ", "")) * 0.3


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #


NOT_FOUND = "❓ (ترجمه یافت نشد)"


def _normalize(text: str) -> str:
    """Trim the punctuation the translation memory tends to carry around."""
    return text.strip().strip(" .،؛:!?\"'").strip()


def _dictionary_senses(word: str) -> list[str]:
    """Ask MyMemory's translation memory for entries stored under this exact word.

    MyMemory returns many stored segments. Only the ones whose source segment is
    the bare word are dictionary entries ("Yawn" -> "خمیازه"); segments such as
    "yawn." come from running text and give a conjugated phrase
    ("دهن دره کردن") instead, so they are dropped. Best senses first.
    """
    matches = MyMemoryTranslator(source="en-GB", target="fa-IR").translate(word, return_all=True)

    def rank(entry: dict) -> tuple[float, float, float]:
        def num(key: str) -> float:
            try:
                return float(entry.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        return num("match"), num("quality"), num("usage-count")

    senses: list[str] = []
    for entry in sorted((m for m in matches if isinstance(m, dict)), key=rank, reverse=True):
        # Whitespace only — do NOT strip punctuation here, that is the signal.
        if str(entry.get("segment", "")).strip().casefold() != word.casefold():
            continue
        candidate = _normalize(str(entry.get("translation", "")))
        # Skip empties and untranslated echoes (a Latin-only "translation").
        if not candidate or not _PERSIAN_RE.search(candidate):
            continue
        if candidate not in senses:
            senses.append(candidate)

    return senses


# Memory entries sometimes pack two glosses into one string
# ("آشکارکردن - لخت کردن"). Those read badly as a single answer.
_COMPOUND_RE = re.compile(r"\s[-–—/]\s|[،؛]")


def _machine_translation(word: str) -> str:
    """Google's free web endpoint — the fallback when the memory has no entry."""
    return _normalize(GoogleTranslator(source="en", target="fa").translate(word) or "")


@lru_cache(maxsize=8192)
def translate_word(word: str) -> str:
    """Translate a single English term to Persian. Blocking; cached.

    Each word is looked up on its own so the engines treat it as a dictionary
    entry rather than a sentence to translate in context.
    """
    entries: list[str] = []
    try:
        entries = _dictionary_senses(word)
    except Exception as exc:  # quota exhausted, network hiccup, API change
        log.warning("MyMemory lookup failed for %r: %s", word, exc)

    clean = [e for e in entries if not _COMPOUND_RE.search(e)]
    packed = [_normalize(p) for e in entries if _COMPOUND_RE.search(e) for p in _COMPOUND_RE.split(e)]

    # A clean dictionary gloss wins outright; no need to ask Google as well.
    if clean and not MULTI_SENSE:
        return clean[0]

    machine = ""
    try:
        machine = _machine_translation(word)
    except Exception as exc:
        log.warning("Google lookup failed for %r: %s", word, exc)

    ordered: list[str] = []
    for sense in [*clean, machine, *packed]:
        if sense and sense not in ordered:
            ordered.append(sense)

    if not ordered:
        return NOT_FOUND

    return "، ".join(ordered[:MAX_SENSES]) if MULTI_SENSE else ordered[0]


async def translate_words(words: list[str]) -> list[tuple[str, str]]:
    """Translate every term concurrently, preserving the original order."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(word: str) -> tuple[str, str]:
        async with semaphore:
            return word, await asyncio.to_thread(translate_word, word)

    return await asyncio.gather(*(one(w) for w in words))


def format_result(pairs: list[tuple[str, str]]) -> str:
    """word / translation / dashed line, exactly as in the spec."""
    blocks = [f"{word}\n{meaning}" for word, meaning in pairs]
    return f"\n{SEPARATOR}\n".join(blocks)


def split_for_telegram(text: str) -> list[str]:
    """Chunk a long answer on separator boundaries so nothing is cut mid-word."""
    if len(text) <= TELEGRAM_MAX_CHARS:
        return [text]

    chunks: list[str] = []
    current = ""

    for block in text.split(f"\n{SEPARATOR}\n"):
        candidate = block if not current else f"{current}\n{SEPARATOR}\n{block}"
        if len(candidate) > TELEGRAM_MAX_CHARS and current:
            chunks.append(current)
            current = block
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

START_TEXT = (
    "👋 <b>TransTele</b> — English ➜ Persian word list translator.\n\n"
    "Send me a list of English words, <b>one per line</b>:\n"
    "<pre>Yawn\nBare\nFin</pre>\n"
    "and I'll send each word back with its Persian meaning underneath.\n\n"
    "Works in DM, in groups, and in channels where I'm an admin.\n"
    "Commands: /help · /id"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_html(START_TEXT)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_html(
        f"Chat ID: <code>{chat.id}</code>\nType: <code>{chat.type}</code>"
    )


async def handle_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shared entry point for private chats, groups, and channel posts."""
    message = update.effective_message
    if message is None or not message.text:
        return

    is_channel = update.effective_chat.type == ChatType.CHANNEL

    # In a channel the bot sees its own posts again — don't translate them.
    if is_channel and looks_like_output(message.text):
        return

    words = extract_words(message.text)
    if not words:
        return

    if len(words) > MAX_WORDS_PER_MESSAGE:
        if not is_channel:
            await message.reply_text(
                f"That's {len(words)} words. Please send at most "
                f"{MAX_WORDS_PER_MESSAGE} at a time."
            )
        return

    if not is_channel:
        await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    try:
        pairs = await translate_words(words)
    except Exception:
        log.exception("translation failed")
        if not is_channel:
            await message.reply_text("⚠️ Translation service is unreachable. Try again in a moment.")
        return

    result = format_result(pairs)

    if is_channel and CHANNEL_MODE == "edit":
        try:
            await message.edit_text(result)
            return
        except TelegramError as exc:
            # Needs "Edit messages of others" rights, and fails for long posts.
            log.warning("edit failed, falling back to reply: %s", exc)

    for chunk in split_for_telegram(result):
        await message.reply_text(chunk)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    if not BOT_TOKEN:
        sys.exit(
            "BOT_TOKEN is not set.\n"
            "  PowerShell:  $env:BOT_TOKEN = '123456:ABC...'\n"
            "  bash:        export BOT_TOKEN='123456:ABC...'\n"
            "  or put BOT_TOKEN=123456:ABC... in a .env file next to bot.py"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))

    # DMs and groups.
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & ~filters.UpdateType.EDITED
            & (filters.ChatType.PRIVATE | filters.ChatType.GROUPS),
            handle_words,
        )
    )
    # Channel posts (the bot must be an admin of the channel).
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & filters.TEXT & ~filters.COMMAND,
            handle_words,
        )
    )

    app.add_error_handler(on_error)

    log.info("TransTele is running (channel mode: %s). Press Ctrl+C to stop.", CHANNEL_MODE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
