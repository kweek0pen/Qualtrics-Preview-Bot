# How the preview bot works (technical notes)

One command, from this folder:

```bash
./run.sh
```

That runs 12 passes through your survey preview and writes everything to
`runs/<timestamp>/`. Any flag below can be added, e.g. `./run.sh --runs 1`.

What `run.sh` does for you:

1. Checks the Python packages and the headless browser, installing them if missing.
2. Loads `OPENAI_API_KEY` and `QUALTRICS_PREVIEW_URL` from `.env` in this folder. If either is missing it asks once
   (input hidden) and offers to save it there. `.env` is git-ignored.
3. Keeps the Mac from sleeping while the batch runs (`caffeinate -i`). The display can still turn off.
4. Opens the preview in **its own Safari window**. If Qualtrics asks for a login,
   sign in there (including 2FA); the batch starts by itself once the survey loads.

After the login you don't need to touch anything. You can minimise that Safari window,
use other apps or Safari windows, or let the display sleep. Don't close the bot's
window and don't browse in it.

Preview mode records no response rows. Never point this at a live distribution link.
The runner refuses any URL without `/preview/` in it.

## How it works

| Part | Where it runs | How |
|---|---|---|
| Survey pages (opinion, sliders, anything after the chat) | Safari, logged in | `do JavaScript` through AppleScript (`safari_bridge.py`). Works in the background. |
| Chatbot conversation | Headless Chromium (`emma_chat_headless.py`) | Loads the real chat app with the survey's own `chatParams` and types into its real text box |
| Handing the chat back | Safari | Replays the chat app's final message into the survey page, so Qualtrics stores `appData` and shows Next the usual way |

Why the chat runs outside Safari: the chat app lives in a cross-origin iframe, and
Safari won't let scripts type into one. The old approach clicked at guessed screen
coordinates. That meant Safari had to stay in front with the screen on, and clicks
missed once the chat grew taller than the window. The chat app's own code
(`requestData` → parameters, then the transcript posted back to the parent) lets
the bot run the same app with the same system prompt, model, key and end logic
without a screen. See the docstring at the top of `emma_chat_headless.py`.

Between runs the bot clears `appData`, `chatParams` and `hideTextbox` from the
survey's localStorage. The survey rebuilds the chatbot's `initialMessages` from the last
`appData`. Without that reset, run 2's chatbot would carry on run 1's debate.

## One-time Safari setup

1. Safari → Settings → Advanced → **Show features for web developers**
2. Safari → Settings → Developer → **Allow JavaScript from Apple Events**
3. Approve the macOS prompt that lets your terminal control Safari. If you missed it, go to
   System Settings → Privacy & Security → Automation.

Accessibility permission is no longer needed. The bot doesn't click or type at the OS level.

## Outputs (in `runs/<timestamp>/`)

| File | Contents |
|---|---|
| `chat_run_<n>_<timestamp>.json` | Persona, assigned opinion, the position the chatbot was told to argue against, survey answers, chat turns (clean text, plus `raw` when the chatbot's message held hidden search markup), issues |
| `qualtrics_chat_transcript.xlsx` | Summary, Conversation, Survey and Issues sheets |
| `CHAT_CHALLENGES.md` | Researcher-readable notes on chatbot difficulties and repeated chatbot lines |

`runs/last_run.log` holds the console output of the most recent batch.

**All conversations in one place:** `conversation_logs_aggregated.xlsx` in this folder
is updated after every run, across all batches. It has the same four sheets, with the
Run column showing `<batch folder> #<run>`. The list of runs it includes is kept in
`runs/aggregated_runs.txt`. Delete a line there to drop that run the next time the
workbook updates. If the workbook is open in Excel during an update, the run is kept
on the list and shows up in the next update.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--runs N` | `12` | Passes through the survey (study target 10–15) |
| `--preview-url URL` | `QUALTRICS_PREVIEW_URL` in `.env` | Another preview link |
| `--persona FILE` | `persona_hs_political.json` | Participant persona |
| `--opinions FILE` | `opinion_pool.json` | Verbatim opening-opinion pool |
| `--model NAME` | `gpt-4o-mini` | OpenAI model for the participant |
| `--seed N` | none | Reproduce the opinion draw order |
| `--out-dir DIR` | `runs/<timestamp>` | Where logs and reports go |
| `--show-chat` | off | Show the chat browser window, for debugging |

## Tests

```bash
./venv/bin/python tests/test_headless_chat.py
```

This runs the real chat app bundle headless, with only its `/api/chat` backend
faked, through a full 6/6 arc plus the stop keyword. It needs no login and no
model key.

## Report-only commands

```bash
./venv/bin/python export_chat_to_excel.py --out qualtrics_chat_transcript.xlsx
./venv/bin/python write_chat_challenges.py --out CHAT_CHALLENGES.md
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Safari is refusing injected JavaScript` | Turn on "Allow JavaScript from Apple Events" (above) |
| `not authorized to send Apple events` | Allow the terminal under Privacy & Security → Automation |
| Waits at "Qualtrics wants a login" | Sign in in the Safari window the bot opened |
| Issue `chat_load_failed` | The model key in the survey's chat settings was rejected, or the chat app is down. Rerun with `--runs 1 --show-chat` to watch it |
| Issue `handoff_fallback` | The survey didn't react to the chat's final message. The bot stored the transcript and revealed Next itself. Check the survey's message listener |
