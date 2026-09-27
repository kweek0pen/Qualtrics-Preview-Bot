# Qualtrics Preview Bot

A bot that takes your Qualtrics survey **in preview mode**, playing a simulated
participant, and records how it went. It answers the ordinary survey questions,
holds a full conversation with the survey's embedded chatbot, and writes
everything to an Excel workbook and a short report on anything that went wrong.

Use it to test a survey before real participants see it: to check that every page
can be completed, that the chatbot behaves, and that the conversation ends the way
you meant it to.

> **Preview only.** Qualtrics records no responses in preview mode, so the bot
> never touches your real data. It refuses any link without `/preview/` in it.
> Never give it a live distribution link.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Download the bot](#2-download-the-bot)
3. [One-time Safari setup](#3-one-time-safari-setup)
4. [Add your API key and preview link](#4-add-your-api-key-and-preview-link)
5. [Before every run: sign in and open the survey's Build page](#5-before-every-run-sign-in-and-open-the-surveys-build-page)
6. [Run it](#6-run-it)
7. [Where the results go](#7-where-the-results-go)
8. [Make it your own (persona, opinions, options)](#8-make-it-your-own)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. What you need

| | |
|---|---|
| **A Mac with Safari** | The bot drives Safari through AppleScript, so it only runs on macOS. |
| **Python 3.10 or newer** | Check with `python3 --version` in Terminal. If it's missing, install it from [python.org](https://www.python.org/downloads/). |
| **An OpenAI API key** | The bot uses it to write the participant's answers. Get one at [platform.openai.com/api-keys](https://platform.openai.com/api-keys). The default model (`gpt-4o-mini`) is cheap: a full batch usually costs a few cents. |
| **A Qualtrics account that can open the survey** | Preview links only work for people signed in to an account with access to that survey. |

Everything else (the Python packages and a headless browser for the chat) is
installed for you the first time you run the bot.

## 2. Download the bot

In Terminal:

```bash
git clone https://github.com/kweek0pen/Qualtrics-Preview-Bot.git
```

```bash
cd Qualtrics-Preview-Bot
```

Or click **Code → Download ZIP** on GitHub, unzip it, and `cd` into the folder.

## 3. One-time Safari setup

The bot fills in the survey by sending JavaScript to Safari. Safari blocks that
until you allow it:

1. Open Safari → **Settings → Advanced**, and tick **Show features for web developers**.
2. Open Safari → **Settings → Developer**, and tick **Allow JavaScript from Apple Events**.
3. The first time you run the bot, macOS asks whether Terminal may control Safari.
   Click **OK**. (If you missed it: System Settings → Privacy & Security →
   Automation → turn on Safari under Terminal.)

The bot doesn't click or type at the system level, so it doesn't need
Accessibility permission.

## 4. Add your API key and preview link

The bot reads both from a file called `.env` in the bot's folder. That file is
listed in `.gitignore`, so your key stays on your computer and never gets
uploaded to GitHub.

**Make your `.env` file** by copying the example:

```bash
cp .env.example .env
```

Then open `.env` in any text editor (for example `open -e .env`) and fill in the
two lines:

```ini
OPENAI_API_KEY=sk-your-key-here
QUALTRICS_PREVIEW_URL='https://yourschool.qualtrics.com/jfe/preview/previewId/.../SV_...?Q_CHL=preview&Q_SurveyVersionID=current'
```

**Where to find your preview link:**

1. Open your survey in Qualtrics (the **Build** / survey editor page).
2. Click **Preview** at the top of the editor.
3. A new tab opens with the survey. Copy the whole address from that tab's
   address bar. It contains `/preview/` and starts with your school's or
   company's Qualtrics address.
4. Paste it after `QUALTRICS_PREVIEW_URL=`, **inside single quotes** (the link
   contains `&`, which breaks without the quotes).

> Skipping this step is fine too: if either value is missing, `./run.sh` asks
> for it when it starts and offers to save it to `.env` for you. The key is
> typed with the input hidden.

## 5. Before every run: sign in and open the survey's Build page

**This is the step that makes the bot work reliably.** Before you start a batch:

1. Open **Safari** (not Chrome or another browser).
2. **Sign in to Qualtrics**, including any two-factor or university single sign-on step.
3. Open **your survey's Build page** (the survey editor, where you edit questions)
   and leave it open.

Then start the bot (next step). It opens the preview in a Safari window of its
own and goes straight into the survey.

**Why this matters.** Qualtrics preview links only open for a signed-in account,
and many institutions spread one login across several Qualtrics servers
(for example, your account lives on one server while the survey is served from
another). If you open a preview link while signed out, the login can bounce
between those servers ("too many redirects", or an "Oh no, something went
wrong!" page) and never land on the survey. When you are already signed in and
the survey's Build page has loaded, Safari holds a complete session on the
survey's own server, and the preview opens at once with no login in the way.

The bot does have a fallback: if the preview doesn't open, it sends its window to
the survey editor, shows a notification, and waits up to 15 minutes for you to
sign in there. That works most of the time, but signing in first, on the Build
page, works every time.

Qualtrics sign-ins expire after roughly an hour. For long batches, if you see the
bot waiting for a login, sign in again in the bot's window and it carries on.

## 6. Run it

From the bot's folder:

```bash
./run.sh
```

This runs **12 passes** through the survey. To try a single run first (a good
idea the first time):

```bash
./run.sh --runs 1
```

What `run.sh` does for you:

1. Creates a Python environment in `./venv` and installs the packages it needs
   (first run only).
2. Loads your API key and preview link from `.env`, asking for anything missing.
3. Keeps your Mac from sleeping while the batch runs (the display can still turn off).
4. Opens the preview in its own Safari window and works through the survey,
   run after run.

**While it runs** you can minimise the bot's Safari window, use other apps or
other Safari windows, or let the screen turn off. Just don't close the bot's
window or browse in it. A progress log prints in Terminal and is also saved to
`runs/last_run.log`.

To stop early, press **Ctrl + C** in Terminal. Runs that already finished are
kept.

## 7. Where the results go

Each batch gets its own folder, `runs/<date_time>/`:

| File | What's in it |
|---|---|
| `qualtrics_chat_transcript.xlsx` | Summary, Conversation, Survey and Issues sheets for every run in the batch |
| `CHAT_CHALLENGES.md` | A readable report of difficulties in the chatbot conversations and any lines the chatbot repeated |
| `chat_run_<n>_<time>.json` | The full record of one run: persona, assigned opinion, every survey answer, every chat turn, any issues |

`conversation_logs_aggregated.xlsx` in the main folder collects **every run from
every batch** in one workbook, updated after each run. (Close it in Excel while a
batch runs, or the update waits until the next run.)

All of these are git-ignored, since they contain survey content.

## 8. Make it your own

**The participant.** `persona_hs_political.json` describes who the bot pretends
to be: age, education, location, personality and writing style. Edit it, or make
your own file and pass it with `--persona my_persona.json`.

**Opening opinions.** `opinion_pool.json` is a list of opinion statements. Each
run picks one at random and uses it word for word where the survey asks for the
participant's opinion. Replace them with statements for your study, or pass
another file with `--opinions`.

**Limits built in:** the participant writes at most 150 words per chat message
and sends at most 10 chat messages per conversation.

**Options** (add any of these after `./run.sh`):

| Option | Default | What it does |
|---|---|---|
| `--runs N` | `12` | How many times to go through the survey |
| `--preview-url URL` | `QUALTRICS_PREVIEW_URL` from `.env` | Use a different preview link for this batch |
| `--persona FILE` | `persona_hs_political.json` | Participant persona |
| `--opinions FILE` | `opinion_pool.json` | Pool of opening opinions |
| `--model NAME` | `gpt-4o-mini` | OpenAI model that writes the participant's answers |
| `--seed N` | none | Repeat the same random opinion order |
| `--out-dir DIR` | `runs/<date_time>` | Where the results go |
| `--show-chat` | off | Show the chat's browser window, to watch the conversation |

Example:

```bash
./run.sh --runs 3 --model gpt-4o --show-chat
```

**About the chatbot.** The conversation part is built for surveys that embed
their chatbot as a web app in an iframe and pass it its settings through
`chatParams` (the setup this project was made for). The bot loads that same chat
app with your survey's own settings in a hidden browser and types into it, then
hands the finished conversation back to the survey. The regular survey pages
(multiple choice, sliders, text entry) work on any Qualtrics survey. For more
technical detail, see [RUNNING.md](RUNNING.md).

## 9. Troubleshooting

| What you see | What to do |
|---|---|
| `Safari is refusing injected JavaScript` | Turn on **Allow JavaScript from Apple Events** ([step 3](#3-one-time-safari-setup)). |
| `not authorized to send Apple events` | System Settings → Privacy & Security → Automation → allow Terminal to control Safari. |
| The bot waits at "Qualtrics wants a login" | Sign in in the Safari window the bot opened. Next time, sign in and open the Build page first ([step 5](#5-before-every-run-sign-in-and-open-the-surveys-build-page)). |
| "Too many redirects" or "Oh no, something went wrong!" | Refresh the page. Once the survey editor shows, the bot takes over. |
| `Signed in to the editor, but no preview link opened the survey` | Click **Preview** in the editor, copy that tab's link, and run `./run.sh --preview-url '<that link>'` (or update `.env`). |
| `That is not a Qualtrics PREVIEW link` | You pasted a live/anonymous link. Use the link from the editor's **Preview** button. |
| `No preview link` | Add `QUALTRICS_PREVIEW_URL` to `.env` ([step 4](#4-add-your-api-key-and-preview-link)). |
| OpenAI error about the key or quota | Check `OPENAI_API_KEY` in `.env` and that your OpenAI account has billing set up. |
| Issue `chat_load_failed` in the report | The chat app didn't load or rejected its settings. Run `./run.sh --runs 1 --show-chat` to watch it. |
| Issue `handoff_fallback` in the report | The survey didn't react to the chat's final message, so the bot saved the transcript and revealed Next itself. Check the survey's message listener for the chat. |

### Running the tests

```bash
./venv/bin/python tests/test_headless_chat.py
```

This runs the real chat app with only its backend faked, through a full
conversation. It needs no Qualtrics login and no API key.

---

## License

[MIT](LICENSE)
