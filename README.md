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

You can also **reply to any message and mention the bot** — it translates that message and
posts the result under it. See [Calling the bot on a message](#calling-the-bot-on-a-message).

Two translation backends:

| Backend            | When it is used                        | Needs                          |
| ------------------ | -------------------------------------- | ------------------------------ |
| **LLM**            | when `LLM_BASE_URL` is set             | Ollama on your machine, or any OpenAI-compatible API |
| **Free endpoints** | otherwise, and whenever the LLM fails  | nothing                        |

Nothing is required to run the bot: with no LLM configured it behaves exactly as it always
has, through the free public endpoints of MyMemory and Google Translate.

## 1. Install

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

Leave it running — the bot polls Telegram and stops with `Ctrl+C`. On startup it logs which
translation engine it picked:

```
Translation engine: translategemma:27b via ollama API at http://localhost:11434 (word lists + messages)
```

## Using an LLM translator

### Local — Ollama + translategemma:27b

[TranslateGemma](https://ollama.com/library/translategemma) is a translation-only model
family, and the bot speaks its prompt format. Running it on your own machine needs no
account, no key and no quota. Install [Ollama](https://ollama.com/download), then pull the
model:

```bash
ollama pull translategemma:27b
```

Point the bot at it in `.env`:

```
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=translategemma:27b
```

That's the whole setup. Restart the bot and check it with `/engine` in Telegram:

```
Backend: translategemma:27b (ollama API)
Endpoint: http://localhost:11434
Scope: word lists + messages
Test: hello → سلام، درود
```

The smaller copies are drop-in replacements and a lot lighter — `translategemma:4b` (3.3 GB)
and `translategemma:12b` (8.1 GB) against 17 GB for the 27B. They translate less accurately;
see [Notes and limits](#notes-and-limits).

### Local, but through the OpenAI-compatible API

Ollama also serves an OpenAI-style API. Add `/v1` and the bot switches protocol on its own:

```
LLM_BASE_URL=http://localhost:11434/v1
```

### Remote

Any OpenAI-compatible endpoint works — a copy of Ollama on another machine, or a hosted
provider:

```
LLM_BASE_URL=https://api.example.com/v1
LLM_MODEL=some-model
LLM_API_KEY=sk-...
```

`LLM_API=auto` picks the native Ollama API for a plain host and the OpenAI-compatible one
when the URL contains `/v1`. Force it with `LLM_API=ollama` or `LLM_API=openai`.

### If the LLM is missing or breaks

Every request the LLM cannot serve — no `LLM_BASE_URL`, Ollama not running, model not
pulled, timeout, empty answer, HTTP error — falls back to the free MyMemory/Google path and
is answered anyway. The failure is logged, the user sees a translation:

```
2026-08-12 20:14:03 | WARNING | transtele | LLM failed for 'yawn' (All connection attempts failed), falling back
```

## The prompt

The bot sends the [TranslateGemma](https://ollama.com/library/translategemma) prompt format
verbatim — including the two blank lines before the text, which are part of it. Persian is
named by its full locale, `fa-IR`. For a whole message, English ➜ Persian:

```
You are a professional English (en) to Persian (fa-IR) translator. Your goal is to accurately convey the meaning and nuances of the original English text while adhering to Persian grammar, vocabulary, and cultural sensitivities.
Produce only the Persian translation, without any additional explanations or commentary. Please translate the following English text into Persian:


Where is the train station?
```

A word list is not running text, so single words get the same prompt with one sentence
changed — the model is asked for **every synonym**, not one contextual rendering:

```
You are a professional English (en) to Persian (fa-IR) translator. Your goal is to accurately convey the meaning and nuances of the original English text while adhering to Persian grammar, vocabulary, and cultural sensitivities.
Produce only the Persian synonyms of the word, all of them, separated by "،", without any additional explanations or commentary. Please translate the following English word into Persian:


bank
```

So a word list comes back with every sense on one line, instead of a single gloss. Measured
on `translategemma:4b`:

```
bank
بانک، موسسه مالی، مؤسسه اعتباری، صرافی
----------------------------------------------------------
bare
بی‌ پوشش، عریان، خالی، فاقد
----------------------------------------------------------
spring
بهار، فصل بهار
----------------------------------------------------------
good morning
صبح بخیر، صبح زیبا، صبح آراسته
```

The answer is cleaned before it is sent: bullets, `Translation:` prefixes, wrapper quotes and
stray blank lines are stripped, and a bulleted list is folded into one `،`-separated line.

Persian input is detected and translated the other way (`fa-IR` ➜ `en`) with the same
template.

## Calling the bot on a message

**Reply to a message, mention the bot, and it translates that message** and posts the
translation as a reply to the original:

```
Ali:  I will call you tomorrow morning.
You:  ↳ @YourBot
Bot:  ↳ من فردا صبح با شما تماس خواهم گرفت.
```

This works in DMs, in groups, and in channels where the bot is an admin. `/tr` as a reply
does the same thing, and `/tr some text` translates the text you write after it.

- If the message you point at is a **word list**, you get the word/meaning/dashed-line
  layout. Anything else is translated as running text.
- Direction is picked from the script: a Persian message is translated to English.
- Photo captions work too.
- In a **group** this works even with privacy mode on, because Telegram always delivers
  messages that mention the bot by `@username`. Only plain word lists sent without a mention
  need `/setprivacy` → **Disable** in @BotFather.
- In a **channel**, post your reply to the target post with `@YourBot` in it. The bot needs
  **Post Messages** rights.

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

## How the free-endpoint backend works

Used when no LLM is configured, and as the fallback when one is. Each line is looked up **on
its own**, so the engine returns a dictionary entry rather than translating the list as one
sentence.

1. MyMemory's translation memory is queried first. Only entries stored under the exact word
   are used — those are the dictionary glosses (`Yawn` → `خمیازه`). Entries stored under
   `yawn.` come from running text and give conjugated phrases (`دهن دره کردن`), so they are
   discarded.
2. If the memory has nothing clean, Google Translate's free endpoint is used.
3. If both fail, the word is marked `❓ (ترجمه یافت نشد)`.

Results are cached in memory, so repeated words cost nothing. The cache is shared by both
backends.

## Options

All optional, set as environment variables or in `.env`:

| Variable          | Default             | Meaning                                                                 |
| ----------------- | ------------------- | ----------------------------------------------------------------------- |
| `BOT_TOKEN`       | —                   | Required. Token from @BotFather.                                         |
| `CHANNEL_MODE`    | `reply`             | `reply` or `edit`, see above.                                            |
| `LLM_BASE_URL`    | *(empty)*           | Endpoint of the LLM. Empty = free endpoints only.                        |
| `LLM_MODEL`       | `translategemma:27b` | Model name as the endpoint knows it.                                    |
| `LLM_API_KEY`     | *(empty)*           | Bearer token for a remote provider. Local Ollama needs none.             |
| `LLM_API`         | `auto`              | `auto`, `ollama` (native `/api/generate`) or `openai` (`/v1/chat/completions`). |
| `LLM_SCOPE`       | `all`               | `all` = LLM does word lists and messages. `text` = messages only; word lists keep the dictionary lookup. |
| `LLM_TIMEOUT`     | `60`                | Seconds to wait for the model.                                           |
| `LLM_CONCURRENCY` | `2`                 | Prompts in flight at once.                                               |
| `MULTI_SENSE`     | `0`                 | Free-endpoint backend only: `1` prints up to 3 meanings per word.        |

Tunables at the top of `bot.py`: `MAX_WORDS_PER_MESSAGE` (120), `MAX_CONCURRENCY` (5),
`MAX_SENSES` (3), `MAX_TEXT_CHARS` (4000).

## Notes and limits

- **Small copies fumble bare vocabulary.** A single word carries no context, so an ambiguous
  one can be read in the wrong language entirely: `translategemma:4b` answers `fin` with
  `پایان`, taking it for French, where the dictionary lookup correctly gives `باله`. It also
  misses `yawn`. Sentences are much safer. Two ways out: run a bigger copy, or set
  `LLM_SCOPE=text` so word lists keep using the dictionary and the LLM only handles whole
  messages.
- The first LLM request after startup is slow: Ollama has to load the model into memory
  (a few seconds on GPU, up to a minute on CPU). Raise `LLM_TIMEOUT` if it times out.
- `LLM_CONCURRENCY` above 2 rarely helps with one local model — the requests queue inside
  Ollama and every one of them gets slower.
- The free MyMemory endpoint allows roughly 1000 words/day per IP for anonymous use. Past
  that it starts refusing, and the bot silently falls back to Google Translate.
- Long answers are split across several messages at separator boundaries (Telegram caps a
  message at 4096 characters).
- `googletrans` was avoided on purpose: it pins an old `httpx` and breaks regularly.
  `deep-translator` is maintained and needs no key.

## Commands

| Command  | Effect                                                              |
| -------- | ------------------------------------------------------------------- |
| `/start` | Usage help.                                                          |
| `/help`  | Same as `/start`.                                                    |
| `/tr`    | As a reply: translates that message. With text after it: translates the text. |
| `/engine`| Shows the active backend and pings the LLM with a test word.          |
| `/id`    | Prints the current chat's ID and type.                               |
