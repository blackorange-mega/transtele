# TransTele — English ➜ Persian word-list bot

Send a list of English words (one per line) in a DM, a group, or a channel where the bot is
an admin. It replies with each word and its Persian meaning underneath, separated by a
dashed line.

```
Yawn
خمیازه
----------------------------------------------------------
Bare
برهنه
----------------------------------------------------------
Fin
باله
```

No API key and no paid service. Translation goes through `deep-translator`, which uses the
free public endpoints of MyMemory and Google Translate.

## 1. Install

```bash
pip install python-telegram-bot deep-translator python-dotenv
```

or

```bash
pip install -r requirements.txt
```

Python 3.10+ (tested on 3.13).

## 2. Get a bot token

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, pick a name and a username.
3. Copy the token it gives you (looks like `123456789:AAE...`).

## 3. Configure

Copy `.env.example` to `.env` and paste your token:

```
BOT_TOKEN=123456789:AAE...
CHANNEL_MODE=reply
```

Or set it in the shell instead:

```bash
export BOT_TOKEN="123456789:AAE..."
```

PowerShell:

```bash
$env:BOT_TOKEN = "123456789:AAE..."
```

## 4. Run

```bash
python bot.py
```

Leave it running — the bot polls Telegram and stops with `Ctrl+C`.

## Using it in a channel

1. Open your channel → **Administrators** → **Add Admin** → pick your bot.
2. Give it **Post Messages** (and **Edit Messages** if you want `CHANNEL_MODE=edit`).
3. Post a list of English words in the channel. The bot answers under the post.

`CHANNEL_MODE` controls what it does with a channel post:

| Value   | Behaviour                                                                |
| ------- | ------------------------------------------------------------------------ |
| `reply` | Posts the translation as a new message under the original post (default). |
| `edit`  | Rewrites the original post in place. Needs *Edit Messages* rights; falls back to `reply` if the edit fails. |

The bot ignores posts that already contain Persian text or the dashed separator, so its own
messages never trigger another round of translation.

## Using it in a group

Add the bot to the group. By default Telegram bots only see messages addressed to them
("privacy mode"). To let it read every message, send `/setprivacy` to @BotFather, choose the
bot, and select **Disable**.

## How the translation works

Each line is looked up **on its own**, so the engine returns a dictionary entry rather than
translating the list as one sentence.

1. MyMemory's translation memory is queried first. Only entries stored under the exact word
   are used — those are the dictionary glosses (`Yawn` → `خمیازه`). Entries stored under
   `yawn.` come from running text and give conjugated phrases (`دهن دره کردن`), so they are
   discarded.
2. If the memory has nothing clean, Google Translate's free endpoint is used.
3. If both fail, the word is marked `❓ (ترجمه یافت نشد)`.

Results are cached in memory, so repeated words cost nothing.

## Options

All optional, set as environment variables or in `.env`:

| Variable       | Default | Meaning                                                              |
| -------------- | ------- | -------------------------------------------------------------------- |
| `BOT_TOKEN`    | —       | Required. Token from @BotFather.                                      |
| `CHANNEL_MODE` | `reply` | `reply` or `edit`, see above.                                         |
| `MULTI_SENSE`  | `0`     | `1` prints up to 3 meanings per word (`خمیازه، خمیازه بکش`).          |

Tunables at the top of `bot.py`: `MAX_WORDS_PER_MESSAGE` (120), `MAX_CONCURRENCY` (5),
`MAX_SENSES` (3).

## Notes and limits

- The free MyMemory endpoint allows roughly 1000 words/day per IP for anonymous use. Past
  that it starts refusing, and the bot silently falls back to Google Translate.
- Concurrency is deliberately capped at 5 words at a time; the free endpoints throttle
  clients that hammer them.
- Long answers are split across several messages at separator boundaries (Telegram caps a
  message at 4096 characters).
- `googletrans` was avoided on purpose: it pins an old `httpx` and breaks regularly.
  `deep-translator` is maintained and needs no key.

## Commands

| Command  | Effect                                |
| -------- | ------------------------------------- |
| `/start` | Usage help.                            |
| `/help`  | Same as `/start`.                      |
| `/id`    | Prints the current chat's ID and type. |
