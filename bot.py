"""
TransTele — English -> Persian word-by-word translator bot.

Send a list of English words (one per line) in a DM, a group, or a channel where
the bot is an admin. The bot replies with each word followed by its Persian
meaning, separated by a dashed line. Reply to any message and mention the bot
(or use /tr) and it translates that message instead.

Two translation backends:

* An LLM served over an HTTP API — local (Ollama on your own machine) or remote
  (any OpenAI-compatible endpoint). Enabled by setting LLM_BASE_URL.
* `deep-translator`, which talks to the free public endpoints of MyMemory and
  Google Translate. No API key, no billing account.

The LLM is used when it is configured and reachable; otherwise every request
falls back to the free endpoints, so the bot never stops working.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from functools import lru_cache

import httpx
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

# Longest message the bot will translate as running text (reply mode).
MAX_TEXT_CHARS = 4000

# MULTI_SENSE=1 prints several meanings per word ("خمیازه، دهن‌دره") instead of
# just the best one. Applies to the free-endpoint backend; the LLM backend is
# asked for every synonym anyway.
MULTI_SENSE = os.getenv("MULTI_SENSE", "0").strip() in {"1", "true", "yes", "on"}
MAX_SENSES = 3

# --- LLM backend (optional) ------------------------------------------------ #
# Empty LLM_BASE_URL == no LLM == the bot behaves exactly as it did before.
#   Ollama on this machine   : http://localhost:11434
#   Ollama, OpenAI-compatible: http://localhost:11434/v1
#   A hosted provider        : https://api.example.com/v1  (+ LLM_API_KEY)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "translategemma:27b").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()

# "auto" picks the OpenAI-compatible protocol when the URL already contains
# /v1, and Ollama's native /api/generate otherwise.
LLM_API = os.getenv("LLM_API", "auto").strip().lower()

# What the LLM is used for:
#   "all"  -> word lists and whole messages (default)
#   "text" -> whole messages only; single words keep the dictionary lookup,
#             which is more reliable for bare vocabulary on small models
LLM_SCOPE = os.getenv("LLM_SCOPE", "all").strip().lower()

LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))

# A 4B model on a laptop GPU answers one prompt at a time; asking for five in
# parallel just makes every one of them slower.
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "2"))

LLM_ENABLED = bool(LLM_BASE_URL)

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
_LATIN_RE = re.compile(r"[A-Za-z]")
_SPLIT_RE = re.compile(r"[,،;]")


def strip_mentions(text: str, username: str | None) -> str:
    """Remove "@thisbot" from a message so it is not treated as a word."""
    if not username:
        return text
    return re.sub(rf"@{re.escape(username)}\b", " ", text, flags=re.IGNORECASE)


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

        parts = [p.strip() for p in _SPLIT_RE.split(line)] if _SPLIT_RE.search(line) else [line]

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


def is_word_list(text: str) -> bool:
    """True if the text reads as a vocabulary list rather than running text.

    Decides how a message pointed at with a reply is handled: word-by-word with
    synonyms, or translated as a whole.
    """
    lines = [line for line in (ln.strip() for ln in text.splitlines()) if line]
    if not lines:
        return False

    for line in lines:
        for part in _SPLIT_RE.split(line):
            part = _BULLET_RE.sub("", part).strip(" \t\"'.:!?")
            if part and len(part.split()) > 2:
                return False

    return True


def detect_direction(text: str) -> tuple[str, str]:
    """Pick (source, target) from the script the text is written in.

    English in, Persian out — and the other way round when someone points the
    bot at a Persian message.
    """
    if len(_PERSIAN_RE.findall(text)) > len(_LATIN_RE.findall(text)):
        return "fa", "en"
    return "en", "fa"


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
# LLM backend
# --------------------------------------------------------------------------- #

LANG_NAMES = {"en": "English", "fa": "Persian"}
LIST_SEPARATOR = {"fa": "، "}

# Locale tags for the prompt. Persian is named as fa-IR so the model is told
# which variant to produce. Google's free endpoint only accepts the bare "fa",
# so this map is for the prompt (and MyMemory) alone.
LANG_CODES = {"en": "en", "fa": "fa-IR"}

# TranslateGemma's documented prompt. The model was trained on this exact
# wording, including the two blank lines in front of the text — keep them.
_PROMPT_INTRO = (
    "You are a professional {src_name} ({src}) to {tgt_name} ({tgt}) translator. "
    "Your goal is to accurately convey the meaning and nuances of the original "
    "{src_name} text while adhering to {tgt_name} grammar, vocabulary, and "
    "cultural sensitivities.\n"
)

_PROMPT_TEXT = (
    "Produce only the {tgt_name} translation, without any additional "
    "explanations or commentary. Please translate the following {src_name} "
    "text into {tgt_name}:\n\n\n{text}"
)

# Same shape, but the answer we want for a vocabulary entry is the set of
# synonyms rather than one contextual rendering.
_PROMPT_WORD = (
    "Produce only the {tgt_name} synonyms of the word, all of them, separated "
    'by "{sep}", without any additional explanations or commentary. Please '
    "translate the following {src_name} word into {tgt_name}:\n\n\n{text}"
)


def _prompt(template: str, text: str, src: str, tgt: str) -> str:
    return (_PROMPT_INTRO + template).format(
        src=LANG_CODES.get(src, src),
        tgt=LANG_CODES.get(tgt, tgt),
        src_name=LANG_NAMES.get(src, src),
        tgt_name=LANG_NAMES.get(tgt, tgt),
        sep=LIST_SEPARATOR.get(tgt, ", ").strip(),
        text=text,
    )


def build_word_prompt(word: str, src: str, tgt: str) -> str:
    return _prompt(_PROMPT_WORD, word, src, tgt)


def build_text_prompt(text: str, src: str, tgt: str) -> str:
    return _prompt(_PROMPT_TEXT, text, src, tgt)


def llm_flavor() -> str:
    """Which wire protocol to speak: "openai" or "ollama"."""
    if LLM_API in {"openai", "ollama"}:
        return LLM_API
    return "openai" if "/v1" in LLM_BASE_URL else "ollama"


def _openai_url() -> str:
    base = LLM_BASE_URL if LLM_BASE_URL.endswith("/v1") else f"{LLM_BASE_URL}/v1"
    return f"{base}/chat/completions"


_http: httpx.AsyncClient | None = None
_llm_gate: asyncio.Semaphore | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=LLM_TIMEOUT)
    return _http


def _gate() -> asyncio.Semaphore:
    global _llm_gate
    if _llm_gate is None:
        _llm_gate = asyncio.Semaphore(max(1, LLM_CONCURRENCY))
    return _llm_gate


async def llm_generate(prompt: str) -> str:
    """Send one prompt to the configured endpoint and return the raw answer."""
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}

    if llm_flavor() == "openai":
        url = _openai_url()
        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "stream": False,
        }
    else:
        url = f"{LLM_BASE_URL}/api/generate"
        payload = {
            "model": LLM_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        }

    async with _gate():
        response = await _client().post(url, json=payload, headers=headers)

    response.raise_for_status()
    data = response.json()

    if llm_flavor() == "openai":
        return str(data["choices"][0]["message"]["content"] or "")
    return str(data.get("response") or "")


# Prefixes a chatty model likes to put in front of the answer.
_PREFIX_RE = re.compile(
    r"^\s*(?:translation|persian|farsi|english|answer|output|ترجمه|معنی)\s*[:：]\s*",
    re.IGNORECASE,
)


def _clean_line(line: str) -> str:
    line = _BULLET_RE.sub("", line)
    line = _PREFIX_RE.sub("", line)
    return line.strip().strip("*_`").strip(" \t\"'.،؛:!?").strip()


def clean_llm_text(raw: str) -> str:
    """Tidy a full-text answer: drop wrapper quotes and label prefixes."""
    lines = [_PREFIX_RE.sub("", ln).strip() for ln in raw.strip().splitlines()]
    return "\n".join(lines).strip().strip('"“”').strip()


def clean_llm_senses(raw: str, tgt: str) -> str:
    """Tidy a word answer into one line of synonyms.

    Models answer either "a، b، c" on one line or as a bulleted list; both end
    up as the same single line here.
    """
    separator = LIST_SEPARATOR.get(tgt, ", ")

    pieces: list[str] = []
    for line in raw.strip().splitlines():
        for part in _SPLIT_RE.split(_clean_line(line)):
            part = _clean_line(part)
            if part and part not in pieces:
                pieces.append(part)

    return separator.join(pieces)


# --------------------------------------------------------------------------- #
# Free-endpoint backend (the fallback, and the default when no LLM is set up)
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


def _machine_translation(text: str, src: str, tgt: str) -> str:
    """Google's free web endpoint — the fallback when the memory has no entry."""
    return _normalize(GoogleTranslator(source=src, target=tgt).translate(text) or "")


@lru_cache(maxsize=8192)
def free_translate_word(word: str, src: str = "en", tgt: str = "fa") -> str:
    """Translate a single term with the free endpoints. Blocking; cached.

    Each word is looked up on its own so the engines treat it as a dictionary
    entry rather than a sentence to translate in context.
    """
    entries: list[str] = []
    if (src, tgt) == ("en", "fa"):
        # The translation memory is only mined for the direction it is good at.
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
        machine = _machine_translation(word, src, tgt)
    except Exception as exc:
        log.warning("Google lookup failed for %r: %s", word, exc)

    ordered: list[str] = []
    for sense in [*clean, machine, *packed]:
        if sense and sense not in ordered:
            ordered.append(sense)

    if not ordered:
        return NOT_FOUND

    return "، ".join(ordered[:MAX_SENSES]) if MULTI_SENSE else ordered[0]


@lru_cache(maxsize=512)
def free_translate_text(text: str, src: str = "en", tgt: str = "fa") -> str:
    """Whole-message translation with the free endpoints. Blocking; cached."""
    try:
        return _machine_translation(text, src, tgt) or NOT_FOUND
    except Exception as exc:
        log.warning("Google text lookup failed: %s", exc)
        return NOT_FOUND


# --------------------------------------------------------------------------- #
# Translation front end — LLM first, free endpoints as the safety net
# --------------------------------------------------------------------------- #

_cache: dict[tuple[str, str, str, str], str] = {}


def _cached(kind: str, text: str, src: str, tgt: str) -> str | None:
    return _cache.get((kind, text.casefold(), src, tgt))


def _remember(kind: str, text: str, src: str, tgt: str, value: str) -> None:
    if len(_cache) > 8192:
        _cache.clear()
    _cache[(kind, text.casefold(), src, tgt)] = value


async def translate_word(word: str, src: str = "en", tgt: str = "fa") -> str:
    """One term -> its meanings in the target language."""
    hit = _cached("word", word, src, tgt)
    if hit is not None:
        return hit

    if LLM_ENABLED and LLM_SCOPE != "text":
        try:
            answer = clean_llm_senses(await llm_generate(build_word_prompt(word, src, tgt)), tgt)
            if answer:
                _remember("word", word, src, tgt, answer)
                return answer
            log.warning("LLM returned nothing for %r, falling back", word)
        except Exception as exc:
            log.warning("LLM failed for %r (%s), falling back", word, exc)

    answer = await asyncio.to_thread(free_translate_word, word, src, tgt)
    _remember("word", word, src, tgt, answer)
    return answer


async def translate_text(text: str, src: str = "en", tgt: str = "fa") -> str:
    """A whole message -> its translation."""
    hit = _cached("text", text, src, tgt)
    if hit is not None:
        return hit

    if LLM_ENABLED:
        try:
            answer = clean_llm_text(await llm_generate(build_text_prompt(text, src, tgt)))
            if answer:
                _remember("text", text, src, tgt, answer)
                return answer
            log.warning("LLM returned nothing for a text request, falling back")
        except Exception as exc:
            log.warning("LLM failed for a text request (%s), falling back", exc)

    answer = await asyncio.to_thread(free_translate_text, text, src, tgt)
    _remember("text", text, src, tgt, answer)
    return answer


async def translate_words(
    words: list[str], src: str = "en", tgt: str = "fa"
) -> list[tuple[str, str]]:
    """Translate every term concurrently, preserving the original order."""
    limit = LLM_CONCURRENCY if (LLM_ENABLED and LLM_SCOPE != "text") else MAX_CONCURRENCY
    semaphore = asyncio.Semaphore(max(1, limit))

    async def one(word: str) -> tuple[str, str]:
        async with semaphore:
            return word, await translate_word(word, src, tgt)

    return list(await asyncio.gather(*(one(w) for w in words)))


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

    # A single block can still be too long (whole-message translation).
    sized: list[str] = []
    for chunk in chunks:
        while len(chunk) > TELEGRAM_MAX_CHARS:
            cut = chunk.rfind("\n", 0, TELEGRAM_MAX_CHARS)
            cut = cut if cut > 0 else TELEGRAM_MAX_CHARS
            sized.append(chunk[:cut])
            chunk = chunk[cut:].lstrip("\n")
        if chunk:
            sized.append(chunk)

    return sized


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

START_TEXT = (
    "👋 <b>TransTele</b> — English ➜ Persian word list translator.\n\n"
    "Send me a list of English words, <b>one per line</b>:\n"
    "<pre>Yawn\nBare\nFin</pre>\n"
    "and I'll send each word back with its Persian meaning underneath.\n\n"
    "Or <b>reply to any message</b> and mention me (or send <code>/tr</code>) — "
    "I'll translate that message and post it under the original.\n\n"
    "Works in DM, in groups, and in channels where I'm an admin.\n"
    "Commands: /help · /engine · /id"
)


def engine_name() -> str:
    if not LLM_ENABLED:
        return "free endpoints (MyMemory + Google Translate)"
    scope = "word lists + messages" if LLM_SCOPE != "text" else "messages only"
    return f"{LLM_MODEL} via {llm_flavor()} API at {LLM_BASE_URL} ({scope})"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_html(START_TEXT)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_html(
        f"Chat ID: <code>{chat.id}</code>\nType: <code>{chat.type}</code>"
    )


async def cmd_engine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the active backend, and prove the LLM answers."""
    message = update.effective_message

    if not LLM_ENABLED:
        await message.reply_html(
            "Backend: <b>free endpoints</b> (MyMemory + Google Translate).\n"
            "Set <code>LLM_BASE_URL</code> to use an LLM instead."
        )
        return

    if update.effective_chat.type != ChatType.CHANNEL:
        await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    try:
        sample = clean_llm_senses(await llm_generate(build_word_prompt("hello", "en", "fa")), "fa")
        await message.reply_html(
            f"Backend: <b>{LLM_MODEL}</b> ({llm_flavor()} API)\n"
            f"Endpoint: <code>{LLM_BASE_URL}</code>\n"
            f"Scope: {'word lists + messages' if LLM_SCOPE != 'text' else 'messages only'}\n"
            f"Test: <code>hello</code> → {sample or '(empty answer)'}"
        )
    except Exception as exc:
        await message.reply_html(
            f"⚠️ <b>{LLM_MODEL}</b> at <code>{LLM_BASE_URL}</code> is not answering:\n"
            f"<code>{exc}</code>\n\nRequests fall back to the free endpoints."
        )


async def send_translation(update: Update, target_message, text: str) -> None:
    """Reply to `target_message`, splitting long answers."""
    for chunk in split_for_telegram(text):
        await target_message.reply_text(chunk)


async def translate_and_reply(update, context, target_message, source_text: str) -> None:
    """Translate one message's text and post it under that message.

    A vocabulary list keeps the word/meaning/dashed-line layout; anything else
    is translated as running text.
    """
    is_channel = update.effective_chat.type == ChatType.CHANNEL
    source_text = source_text.strip()

    if not source_text:
        if not is_channel:
            await target_message.reply_text("That message has no text I can translate.")
        return

    if len(source_text) > MAX_TEXT_CHARS:
        if not is_channel:
            await target_message.reply_text(
                f"That message is {len(source_text)} characters. "
                f"I translate at most {MAX_TEXT_CHARS} at a time."
            )
        return

    src, tgt = detect_direction(source_text)

    if not is_channel:
        await context.bot.send_chat_action(chat_id=target_message.chat_id, action="typing")

    try:
        if is_word_list(source_text):
            words = extract_words(source_text)[:MAX_WORDS_PER_MESSAGE]
            if not words:
                return
            result = format_result(await translate_words(words, src, tgt))
        else:
            result = await translate_text(source_text, src, tgt)
    except Exception:
        log.exception("translation failed")
        if not is_channel:
            await target_message.reply_text("⚠️ Translation failed. Try again in a moment.")
        return

    await send_translation(update, target_message, result)


async def cmd_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tr — as a reply, translates the replied-to message; else its own text."""
    message = update.effective_message
    target = message.reply_to_message

    if target is not None:
        await translate_and_reply(update, context, target, target.text or target.caption or "")
        return

    text = " ".join(context.args or []).strip()
    if not text:
        await message.reply_text(
            "Reply to a message with /tr (or a mention) and I'll translate it.\n"
            "You can also write the text after the command: /tr yawn"
        )
        return

    await translate_and_reply(update, context, message, text)


def mentions_bot(message, username: str | None) -> bool:
    """True if the message calls the bot by @username."""
    if not username:
        return False
    text = message.text or message.caption or ""
    return bool(re.search(rf"@{re.escape(username)}\b", text, re.IGNORECASE))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shared entry point for private chats, groups, and channel posts."""
    message = update.effective_message
    if message is None or not message.text:
        return

    username = context.bot.username
    is_channel = update.effective_chat.type == ChatType.CHANNEL

    # "@bot" written as a reply: translate the message it points at.
    if message.reply_to_message is not None and mentions_bot(message, username):
        target = message.reply_to_message
        await translate_and_reply(update, context, target, target.text or target.caption or "")
        return

    # In a channel the bot sees its own posts again — don't translate them.
    if is_channel and looks_like_output(message.text):
        return

    words = extract_words(strip_mentions(message.text, username))
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

    await send_translation(update, message, result)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error", exc_info=context.error)


async def on_shutdown(app: Application) -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


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

    app = Application.builder().token(BOT_TOKEN).post_shutdown(on_shutdown).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("engine", cmd_engine))
    app.add_handler(CommandHandler("tr", cmd_translate))

    # DMs and groups.
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & ~filters.UpdateType.EDITED
            & (filters.ChatType.PRIVATE | filters.ChatType.GROUPS),
            handle_message,
        )
    )
    # Channel posts (the bot must be an admin of the channel).
    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(on_error)

    log.info("Translation engine: %s", engine_name())
    log.info("TransTele is running (channel mode: %s). Press Ctrl+C to stop.", CHANNEL_MODE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
