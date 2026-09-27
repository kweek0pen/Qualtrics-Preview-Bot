#!/usr/bin/env python3
"""
Qualtrics Political-Chat Preview Agent
--------------------------------------
A preview bot for a Qualtrics political-discussion survey. It plays an adult
respondent with a high-school education, answers the ordinary Qualtrics
questions, and then holds the survey's embedded chatbot conversation like a
real participant would.

It is built on top of qualtrics_preview_agent.py, which lives beside it and
supplies all the generic Qualtrics plumbing (iframe handling, question-type
detection, Next-button clicking, login-session saving). Only the parts that are
specific to this survey live here: the persona's assigned political opinion,
the chat-widget handling, and the reporting.

WHAT IS DIFFERENT FROM THE OLD BOT

  1. Every run is assigned ONE political opinion, drawn at random from
     opinion_pool.json, and it is used VERBATIM — the wording in that file is
     the participant's own wording and is never paraphrased by the model.
  2. Chat pages are handled turn by turn instead of being filled in like a text
     question: read what the chatbot said, reply as the persona, send, wait for
     the chatbot's answer, repeat.
  3. Participant chat replies are capped at MAX_REPLY_WORDS words and pushed
     toward a high-school reading level; both are measured, not just requested.
  4. At the end of a batch it writes the Excel workbook and the challenges
     markdown report by itself.

PREVIEW ONLY: Qualtrics preview mode records nothing, so this is safe to run
repeatedly. Never point it at a live distribution link.

SETUP (once):
    ./venv/bin/pip install playwright openai openpyxl
    ./venv/bin/playwright install chromium
    export OPENAI_API_KEY="sk-..."          # keep it in .env, not in this file

    # Preview links are account-protected. If your institution's login loops in Playwright,
    # use safari_runner.py against an already-signed-in Safari tab.

USAGE:
    ./venv/bin/python qualtrics_political_chat_agent.py "<preview url>" \
        --runs 12 --persona persona_hs_political.json

    The study calls for 10-15 passes, so --runs 12 is a sensible default batch.
    There are 10 opinions in the pool and each run draws independently, so some
    opinions repeat across a batch and some may not appear. That is intended.

OUTPUT (per batch, in --out-dir):
    chat_run_<n>_<timestamp>.json   full log: persona, opinion, survey answers,
                                    every chat turn, and the run's chat issues
    chat_run_<n>_stopped_page<N>.png  screenshot, only if a run stops early
    qualtrics_chat_transcript.xlsx  workbook (Summary / Survey / Conversation)
    CHAT_CHALLENGES.md              what was hard about talking to the chatbot
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import Error as PlaywrightError
except ImportError:
    print("Missing dependency. Run:\n  pip install playwright\n  playwright install chromium")
    sys.exit(1)

try:
    from openai import OpenAI
except ImportError:
    print("Missing dependency. Run:\n  pip install openai")
    sys.exit(1)

# The previous bot is the base: everything generic about driving a Qualtrics
# preview is already solved there and is imported rather than re-written.
from qualtrics_preview_agent import (
    ACTION_TIMEOUT_MS,
    POLL_INTERVAL_MS,
    _chat_json,
)
# The old bot's LLM answerer is browser-free, so it is reused as-is for the
# ordinary survey questions; only the page handling had to be rewritten.
from qualtrics_preview_agent import ask_llm as base_ask_llm

import export_chat_to_excel
import write_chat_challenges


# --------------------------------------------------------------------------
# Conversation rules
# --------------------------------------------------------------------------

# The study asks for a short exchange, not an essay contest. Ten participant
# messages is the hard ceiling: after that the bot stops talking and moves the
# survey on, even if the widget would happily keep going.
MAX_CHAT_TURNS = 10
MAX_REPLY_WORDS = 150

# A chatbot backed by an LLM answers in seconds, but a cold start or a long
# streamed reply can take much longer than a normal Qualtrics page swap.
CHAT_REPLY_TIMEOUT_MS = 90000
CHAT_STABLE_POLLS = 3          # streamed replies must stop growing before we read them

OPINION_POOL_FILE = "opinion_pool.json"

# Some institutions' Qualtrics logins send a fresh automation profile into a redirect loop
# (ERR_TOO_MANY_REDIRECTS): the throwaway browser Playwright ships with does not
# hold the cookies that login flow expects. The fix is to drive a real, installed
# browser against a profile folder that persists between runs — log in once by
# hand, and every later run starts already signed in.
BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
DEFAULT_PROFILE_DIR = "browser_profile"


def find_browser(explicit=None):
    """The installed browser to drive. None means Playwright's bundled Chromium,
    which is the one the login loop happens in, so it is the last resort."""
    if explicit:
        if not Path(explicit).exists():
            raise SystemExit(f"No browser at {explicit}")
        return explicit
    for path in BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def open_browser(p, profile_dir, browser_path, headless, slow_ms, engine="webkit"):
    """A persistent context: cookies and the login session live in profile_dir,
    so they survive between runs instead of being thrown away with the browser.

    The engine matters here. Some institutions' Qualtrics logins bounce the account
    across datacenters (survey host -> home datacenter -> back), and every Chromium build tried — the
    bundled one and Brave — lost the session on that hop and died with
    ERR_TOO_MANY_REDIRECTS. The login page says as much: it warns that Chrome is
    not fully supported. Safari completes the same login, so WebKit is the
    default here."""
    profile = Path(profile_dir).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    kwargs = {"user_data_dir": str(profile), "headless": headless, "slow_mo": slow_ms}

    if engine == "chromium":
        # No automation banner, and without the testing flags that help trip
        # the redirect loop.
        kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
        kwargs["ignore_default_args"] = ["--enable-automation"]
        if browser_path:
            kwargs["executable_path"] = browser_path
        return p.chromium.launch_persistent_context(**kwargs)
    if engine == "firefox":
        return p.firefox.launch_persistent_context(**kwargs)
    return p.webkit.launch_persistent_context(**kwargs)


def login_host(preview_url):
    """Qualtrics wants the login on the brand host, not on the preview link —
    going straight to the preview while signed out is what starts the loop."""
    m = re.match(r"(https://[^/]+)", preview_url)
    return f"{m.group(1)}/login" if m else preview_url


def wait_for_preview_access(page, preview_url, wait_seconds=900):
    """Open the preview; if Qualtrics wants a login, hold the batch until the
    human signs in in this same window.

    This is why the batch shares one browser: the Qualtrics session did not survive
    being saved to a profile and reopened, so the reliable way to stay signed in
    is never to close the browser between runs."""
    page.goto(preview_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if "/login" not in page.url:
        return True

    print("\n" + "=" * 70)
    print("Qualtrics wants a login before it will open this preview.")
    print("A browser window is open. Log in there — the batch starts by itself")
    print("as soon as the preview loads. Nothing else is needed.")
    print("=" * 70 + "\n")

    deadline = time.monotonic() + wait_seconds
    last_note = 0.0
    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        try:
            url = page.url
        except PlaywrightError:
            return False
        if "/login" not in url and "qualtrics.com" in url:
            # Signed in somewhere in the account; make sure it is the preview
            # that is open, not the dashboard the login dropped us on.
            if "/jfe/" not in url:
                page.goto(preview_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            if "/login" not in page.url:
                print("Preview is open. Starting the batch.\n")
                return True
        now = time.monotonic()
        if now - last_note > 30:
            last_note = now
            print(f"  ...waiting for the login ({int(deadline - now)}s left)")
    print("Timed out waiting for the login.")
    return False


def save_login_persistent(preview_url, profile_dir, browser_path, slow_ms, engine="webkit"):
    """Open the real browser on the plain login page and wait for the human to
    sign in. Nothing is exported: the persistent profile IS the saved session."""
    with sync_playwright() as p:
        context = open_browser(p, profile_dir, browser_path, False, slow_ms, engine)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(login_host(preview_url), wait_until="domcontentloaded")
        print(f"A browser window is open at {login_host(preview_url)}.")
        print("Log into Qualtrics there. The window closes by itself once you are in.")
        deadline = time.monotonic() + 600
        stable, last_note = 0, 0.0
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            try:
                url = page.url
            except PlaywrightError:
                break
            if "qualtrics.com" in url and "/login" not in url and not url.startswith("about:"):
                stable += 1
            else:
                stable = 0
            if stable >= 3:
                # Confirm the session actually opens the preview, so a run does
                # not fail later on a login wall we could have caught here.
                page.goto(preview_url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                reached = "/login" not in page.url
                print(f"Signed in. Preview link {'opens correctly' if reached else 'STILL redirects to login'}.")
                print(f"Session stored in the profile folder: {Path(profile_dir).resolve()}")
                print("Re-run without --login.")
                context.close()
                return reached
            now = time.monotonic()
            if now - last_note > 20:
                last_note = now
                print(f"  ...still on the login page ({int(deadline - now)}s left).")
        print("Timed out waiting for login.")
        context.close()
        return False

# Typing indicators and other widget chrome that must not be mistaken for the
# chatbot's actual words.
CHAT_NOISE_RE = re.compile(
    r"^\s*(typing|\.{2,}|•+|●+|is typing|thinking|loading|please wait|send|enter)\s*$",
    re.I,
)


# --------------------------------------------------------------------------
# Persona + assigned opinion
# --------------------------------------------------------------------------

DEFAULT_PERSONA = {
    "name": "Joshua Smith",
    "age": 25,
    "gender": "male",
    "education": "two year college graduate",
    "location": "Virginia, United States",
    "personality": ("friendly and willing to talk, opinionated but not aggressive, "
                    "plain everyday words, concrete examples, no "
                    "jargon and no statistics he cannot name a source for"),
    "writing_style": ("short paragraphs, grade 8-10 reading level, contractions, "
                      "occasional sentence fragments, never bulleted lists, never "
                      "more than 150 words"),
}


def load_persona(path):
    if not path:
        return dict(DEFAULT_PERSONA)
    with open(path, "r") as f:
        persona = json.load(f)
    if not isinstance(persona, dict):
        raise ValueError(f"{path}: persona file must contain a JSON object")
    return persona


def load_opinion_pool(path):
    """The opinions are participant-voice statements supplied by the researcher.
    They are used word for word, so this only validates and never rewrites."""
    with open(path, "r") as f:
        pool = json.load(f)
    if not isinstance(pool, list) or not all(isinstance(s, str) and s.strip() for s in pool):
        raise ValueError(f"{path}: expected a JSON list of non-empty strings")
    return pool


def persona_block(persona):
    """The persona description shared by every prompt in a run."""
    lines = [f"{k}: {v}" for k, v in persona.items() if k != "opinion_statement" and v]
    return "\n".join(lines)


def opinion_block(persona):
    opinion = persona.get("opinion_statement", "")
    if not opinion:
        return ""
    return (
        "YOUR POLITICAL OPINION — this is your own settled view for this whole session:\n"
        f"\"{opinion}\"\n"
        "When the survey text box asks what you think about this topic, give this "
        "statement in these exact words. If the chat only asks whether you are ready "
        "to begin, the bot code will send this exact statement once. After that, keep "
        "talking in your own plain everyday words, but never contradict this view and "
        "never abandon it just because the chatbot pushes back."
    )


def _lines(text):
    """Visible chat text, minus the widget's own chrome."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line and not CHAT_NOISE_RE.match(line):
            out.append(line)
    return out


def new_lines_since(before_text, after_text):
    """What the chat added. A chat log only grows, so the common case is a clean
    suffix; the fallback covers widgets that re-render the whole log."""
    before, after = _lines(before_text), _lines(after_text)
    if after[:len(before)] == before:
        return after[len(before):]
    seen = set(before)
    return [ln for ln in after if ln not in seen]


def _strip_own_message(lines, sent_text):
    """The chat echoes the participant's own message into the log; drop it so
    only the chatbot's words are recorded as the chatbot's."""
    sent_norm = re.sub(r"\s+", " ", (sent_text or "")).strip().lower()
    if not sent_norm:
        return lines
    kept = []
    for line in lines:
        norm = re.sub(r"\s+", " ", line).strip().lower()
        if norm and (norm in sent_norm or sent_norm in norm):
            continue
        kept.append(line)
    return kept


# --------------------------------------------------------------------------
# Reply quality: length and reading level
# --------------------------------------------------------------------------

def word_count(text):
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def _syllables(word):
    """Rough syllable count. Exact enough for a grade-level estimate, and it
    keeps this script dependency-free."""
    word = word.lower()
    vowels = "aeiouy"
    count, prev_vowel = 0, False
    for ch in word:
        is_vowel = ch in vowels
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    if word.endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def reading_grade(text):
    """Flesch-Kincaid grade level. The persona finished high school, so replies
    should land roughly in the 6-12 band; anything far above that reads like a
    policy memo and is logged as a problem."""
    sentences = [s for s in re.split(r"[.!?]+", text or "") if s.strip()]
    words = re.findall(r"\b[\w'-]+\b", text or "")
    if not sentences or not words:
        return None
    syllables = sum(_syllables(w) for w in words)
    grade = (0.39 * (len(words) / len(sentences))
             + 11.8 * (syllables / len(words)) - 15.59)
    return round(grade, 1)


def trim_to_words(text, limit):
    """Cut at a sentence boundary if one is close to the limit, otherwise cut at
    the word limit. Used only as a last resort, and always logged."""
    words = re.findall(r"\S+", text)
    if len(words) <= limit:
        return text
    cut = " ".join(words[:limit])
    last_stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if last_stop > len(cut) * 0.6:
        return cut[:last_stop + 1]
    return cut.rstrip(",;:") + "."


def is_opening_opinion_question(question_text):
    """The first survey page asks for the supplied opinion. This must be exact,
    so do not leave it up to the language model."""
    text = (question_text or "").lower()
    return ("political opinion" in text
            and ("challenge" in text or "challenged" in text or "debate" in text))


def chatbot_is_waiting_to_start(text):
    """True only for pre-conversation prompts, not for Emma's first argument.

    The widget sometimes shows Emma's `1/6` argument before the participant has
    typed anything. In that case the first participant reply must respond to the
    argument, not restate the initial survey opinion.
    """
    if not (text or "").strip():
        return True
    low = text.lower()
    if re.search(r"\b[1-6]\s*/\s*6\b", low):
        return False
    starter_bits = [
        "are you ready",
        "ready to begin",
        "ready to start",
        "let me know when",
        "say something",
        "type your",
        "send your",
        "what political opinion",
        "what is your opinion",
    ]
    return any(bit in low for bit in starter_bits)


# --------------------------------------------------------------------------
# Survey questions (non-chat)
# --------------------------------------------------------------------------

# Personal stories ("I've seen friends...", "I know families who...") crept into
# nearly every turn and made the participant sound like the same anecdote on a
# loop. The participant argues about the issue, not about people they know.
_ANECDOTE_RE = re.compile(
    r"\b(I've seen|I have seen|I've watched|I've known|I know [\w' -]{1,30}? who\b|"
    r"I know (some |a few |many )?(people|folks|families|friends|someone|kids|guys|"
    r"women|men|a guy|a girl|a lady)|"
    r"(my|a) (friend|friends|buddy|cousin|brother|sister|mom|dad|mother|father|"
    r"neighbor|coworker|co-worker|family|aunt|uncle|kid|kids|son|daughter)\b|"
    r"people I know|someone I know|in my (life|family|town|area|experience))",
    re.I)


def _opener(text, words=3):
    """The first few words of a message, lower-cased, for spotting a reply
    that starts the same way as an earlier one."""
    text = (text or "").lower().replace("\u2019", "'")
    return " ".join(re.findall(r"[a-z']+", text)[:words])


def _reply_problems(text, own_earlier):
    """Why a drafted participant message is repetitive or too personal."""
    problems = []
    story = _ANECDOTE_RE.search((text or "").replace("\u2019", "'"))
    if story:
        problems.append("it tells a personal story about yourself or people you know "
                        f"(\"{story.group(0)}\")")
    opener = _opener(text)
    if opener and any(_opener(m) == opener for m in own_earlier):
        problems.append(f"it starts with the same words as an earlier message "
                        f"(\"{opener}...\")")
    return problems


def ask_chat_reply(client, model, persona, chat_log, bot_message, turn, issues):
    """One participant message: in character, on topic, short, plain."""
    first_turn = turn == 1
    if first_turn and persona.get("opinion_statement") and chatbot_is_waiting_to_start(bot_message):
        # The opinion is the study's supplied wording, used verbatim, so its
        # reading level is not the persona's and is not flagged.
        text = f"I'm ready. {persona['opinion_statement']}"
        return text, reading_grade(text)

    history_text = "\n".join(
        f"{'YOU' if m['speaker'] == 'participant' else 'EMMA'}: {m['text']}"
        for m in chat_log[-12:]
    ) or "(the conversation has not started yet)"

    own_earlier = [m["text"] for m in chat_log
                   if m.get("speaker") == "participant" and m.get("turn", 0) > 1]
    earlier_block = ("\n".join(f"- {t}" for t in own_earlier)
                     if own_earlier else "(none yet)")
    used_openers = sorted({_opener(t) for t in own_earlier if _opener(t)})

    opening_rule = (
        "This is your FIRST typed chat message, but the chatbot has already made an "
        "argument. Do not restate your opening opinion. Reply directly to the argument "
        "it just made, in your own plain words, and keep your position."
        if first_turn else
        "Reply to the specific argument the chatbot just made: name the actual claim, "
        "fact or example it used and say what you think is wrong with it or what it "
        "leaves out. You may admit a point that lands, but you keep your position."
    )

    prompt = (
        f"You are a real person taking an online survey, not an AI assistant. You are "
        f"having a short political conversation with a chatbot that is arguing hard "
        f"against your view.\n\n"
        f"WHO YOU ARE:\n{persona_block(persona)}\n\n"
        f"{opinion_block(persona)}\n\n"
        f"THE CONVERSATION SO FAR:\n{history_text}\n\n"
        f"YOUR OWN EARLIER MESSAGES (do not repeat these):\n{earlier_block}\n\n"
        f"THE CHATBOT JUST SAID:\n{bot_message or '(nothing yet - you speak first)'}\n\n"
        f"HOW TO WRITE:\n"
        f"- {opening_rule}\n"
        f"- Say something NEW each time: a different reason, question, doubt or "
        f"objection than in your earlier messages. Do not restate a point you already "
        f"made, even in other words.\n"
        f"- Do not open by agreeing or acknowledging (no \"I get that\", \"I see what "
        f"you're saying\", \"I hear you\", \"I understand\"). Start with your actual point, "
        f"a question, or the part of the argument you doubt. Vary how you start"
        + (f"; these openings are already used: {', '.join(repr(o) for o in used_openers)}"
           if used_openers else "") + ".\n"
        f"- Keep it about the issue, not about your private life. No stories about "
        f"yourself, friends, family, neighbours or people you know, and no \"I've seen\" "
        f"or \"I know people who\" examples. Argue from values, common sense, fairness, "
        f"cost, who pays, what could go wrong, or holes in the chatbot's evidence.\n"
        f"- No filler endings like \"it's tough\", \"it's complicated\", \"there's gotta "
        f"be a balance\" or \"we need to find a way\". End on your point or a question.\n"
        f"- At most {MAX_REPLY_WORDS} words. Usually 40 to 100 words; 2 to 5 sentences.\n"
        f"- Your education: {persona.get('education') or 'no college degree'}. Write the "
        f"way you talk: everyday words, contractions, no jargon, no policy-paper "
        f"phrasing.\n"
        f"- No bullet points, no headings, no numbered lists.\n"
        f"- Do not quote statistics or studies you could not name off the top of your "
        f"head. It is fine to say you are not sure about a number.\n"
        f"- Never mention that you are an AI, a model, or role-playing.\n\n"
        f"Return ONLY JSON: {{\"message\": \"<what you type into the chat>\"}}"
    )

    result = _chat_json(client, model, prompt, temperature=0.85)
    text = (result or {}).get("message") if isinstance(result, dict) else None

    if not text:
        issues.append({"turn": turn, "kind": "llm_failed",
                       "detail": f"The model returned no usable message for turn {turn}; "
                                 f"a short neutral holding reply was sent instead."})
        text = "That's a fair point. I still feel the same way about it though."

    text = text.strip()

    problems = _reply_problems(text, own_earlier)
    if problems:
        retry = _chat_json(client, model,
                           f"{prompt}\n\nYour draft was:\n{text}\n\nRewrite it: "
                           + "; ".join(problems) + ".", temperature=0.85)
        retry_text = (retry or {}).get("message") if isinstance(retry, dict) else None
        if retry_text and len(_reply_problems(retry_text, own_earlier)) < len(problems):
            text = retry_text.strip()
        left = _reply_problems(text, own_earlier)
        if left:
            issues.append({"turn": turn, "kind": "repetitive_or_personal",
                           "detail": f"Turn {turn} was still flagged after one rewrite: "
                                     + "; ".join(left) + "."})

    # Length is a requirement of the study, so it is enforced, not just asked for.
    if word_count(text) > MAX_REPLY_WORDS:
        retry = _chat_json(client, model,
                           f"{prompt}\n\nYour previous attempt was {word_count(text)} "
                           f"words, which is too long. Say the same thing in under "
                           f"{MAX_REPLY_WORDS} words.", temperature=0.6)
        retry_text = (retry or {}).get("message") if isinstance(retry, dict) else None
        if retry_text and word_count(retry_text) <= MAX_REPLY_WORDS:
            text = retry_text.strip()
        else:
            issues.append({"turn": turn, "kind": "over_length",
                           "detail": f"Turn {turn} came back at {word_count(text)} words even "
                                     f"after a shorter rewrite was requested, so it was "
                                     f"trimmed to {MAX_REPLY_WORDS} words before sending."})
            text = trim_to_words(text, MAX_REPLY_WORDS)

    grade = reading_grade(text)
    if grade is not None and grade > 12:
        issues.append({"turn": turn, "kind": "reading_level",
                       "detail": f"Turn {turn} scored grade {grade} on Flesch-Kincaid, above "
                                 f"the high-school band the persona writes in."})
    return text, grade


# --------------------------------------------------------------------------
# Finding the survey and the chatbot inside the preview
# --------------------------------------------------------------------------

# The jfe4 preview nests frames two deep:
#   page
#    +- iframe  .../jfe4/previewForm/...   <- the survey (desktop copy)
#    +- iframe  .../jfe4/previewForm/...   <- the same survey, mobile mirror
#         +- iframe  https://<app>.vercel.app  <- Emma Steel, a different origin
# Answering both survey copies would answer everything twice, and the chat is
# cross-origin, which is why this runs on Playwright rather than through
# Safari's AppleScript bridge: only Playwright can reach into that frame.
SURVEY_FRAME_RE = re.compile(r"/jfe\d*/(previewForm|form)/")


def _frame_width(frame):
    try:
        el = frame.frame_element()
        box = el.bounding_box()
        return box["width"] if box else 0
    except PlaywrightError:
        return 0


def survey_frame(page):
    """The widest survey frame: the desktop copy, not the mobile mirror."""
    candidates = [f for f in page.frames if SURVEY_FRAME_RE.search(f.url or "")]
    if not candidates:
        return page.main_frame
    return max(candidates, key=_frame_width)


CHAT_URL_RE = re.compile(r"vercel\.app|vegapunk|sunnyquay|/chat|chatbot|assistant", re.I)


def _chat_params_app_url(page, survey):
    """The chat app's origin, as the survey itself records it.

    Reading it from the page means the detector keeps working when the study is
    re-pointed at a different build. It is not always the survey frame that
    holds it, so every frame gets asked.
    """
    read = ("() => { try { const p = JSON.parse(localStorage.getItem('chatParams'));"
            " return p && p.appURL_; } catch (e) { return null; } }")
    frames = [survey] + [f for f in page.frames if f is not survey]
    for frame in frames:
        try:
            app_url = frame.evaluate(read)
        except PlaywrightError:
            continue
        if app_url:
            return app_url
    return None


def _looks_like_chat(frame):
    """Does this frame contain a chat composer rather than a survey question?

    The fallback for a build served from a host the URL pattern does not know:
    somewhere to type plus a send control that is not Qualtrics' Next button.
    """
    try:
        return bool(frame.evaluate("""() => {
          const typed = [...document.querySelectorAll('textarea, input[type=text]')]
            .filter(e => !e.disabled && !e.readOnly && e.offsetParent !== null);
          if (!typed.length) return false;
          const send = [...document.querySelectorAll('button, [role=button]')].some(b => {
            if (b.id === 'NextButton' || b.id === 'PreviousButton') return false;
            const label = (b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')
                          + ' ' + (b.className || '');
            return /send|submit|→|➤/i.test(label);
          });
          return send;
        }"""))
    except PlaywrightError:
        return False


def chat_frame(page, survey):
    """Emma Steel's frame, whatever it is served from."""
    app_url = _chat_params_app_url(page, survey)
    fallback = None
    for frame in page.frames:
        url = frame.url or ""
        if not url or url == "about:blank" or SURVEY_FRAME_RE.search(url):
            continue
        if frame is survey or frame is page.main_frame:
            continue
        if app_url and url.startswith(app_url):
            return frame
        if CHAT_URL_RE.search(url):
            return frame
        if fallback is None and _looks_like_chat(frame):
            fallback = frame
    return fallback


def chat_arriving(page, survey):
    """Signs a chat frame is on its way in, so waiting longer is worthwhile."""
    frames = [f for f in page.frames
              if (f.url or "") not in ("", "about:blank")
              and not SURVEY_FRAME_RE.search(f.url or "")
              and f is not page.main_frame]
    if frames:
        return True
    if _chat_params_app_url(page, survey):
        return True
    data = read_app_data(survey)
    return bool(data and data.get("messages"))


def wait_for_chat_frame(page, survey, timeout=70):
    """Wait for the chat app's frame to appear and finish booting.

    Qualtrics paints its page before the chat frame is created, and the app
    inside it can take most of a minute to cold-start, so a short wait reports
    "no chatbot" on a page that does in fact have one.
    """
    deadline = time.monotonic() + timeout
    said = False
    while time.monotonic() < deadline:
        chat = chat_frame(page, survey)
        if chat is not None:
            return chat
        arriving = chat_arriving(page, survey)
        if not arriving and time.monotonic() > deadline - timeout + 12:
            return None
        if arriving and not said:
            said = True
            print("    chat frame is still loading...")
        page.wait_for_timeout(1500)
    return None


# --------------------------------------------------------------------------
# Reading and filling survey pages
# --------------------------------------------------------------------------

# Runs inside the survey frame, so `document` is already the right one.
PREAMBLE = r"""
function clean(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
function vis(el) {
  if (!el) return false;
  var r = el.getBoundingClientRect();
  if (r.width <= 1 || r.height <= 1) return false;
  var st = window.getComputedStyle(el);
  return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
}
function blocks() {
  return Array.prototype.slice.call(document.querySelectorAll('.QuestionOuter')).filter(vis);
}
function qtext(b) {
  var t = b.querySelector('.QuestionText, .q-text, legend');
  var s = t ? clean(t.innerText) : '';
  if (s) return s;
  s = clean(b.innerText);
  return s.slice(0, 220);
}
function labelFor(el) {
  if (el.id) {
    var ls = document.querySelectorAll('label');
    for (var i = 0; i < ls.length; i++) {
      if (ls[i].htmlFor === el.id && clean(ls[i].innerText)) return clean(ls[i].innerText);
    }
  }
  var wrap = el.closest('label');
  if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
  var aria = el.getAttribute('aria-label');
  if (clean(aria)) return clean(aria);
  var li = el.closest('li, td, .Selection');
  if (li && clean(li.innerText)) return clean(li.innerText);
  return el.value || '(unlabeled)';
}
function setValue(el, val) {
  // Qualtrics saves on framework events; a bare .value assignment is dropped.
  var proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
}
function matrixRows(b) {
  var rows = b.querySelectorAll('tr'), out = [], names = {};
  for (var i = 0; i < rows.length; i++) {
    var rs = Array.prototype.slice.call(rows[i].querySelectorAll('input[type=radio]')).filter(vis);
    if (rs.length < 2) continue;
    for (var j = 0; j < rs.length; j++) names[rs[j].name || ''] = 1;
    var lab = rows[i].querySelector('th, td:first-child, .RowLabel');
    out.push({label: lab ? clean(lab.innerText) : 'row' + (out.length + 1), n: rs.length});
  }
  return (out.length >= 2 && Object.keys(names).length >= 2) ? out : [];
}
function nextBtn() {
  return document.querySelector('#NextButton, input#NextButton, button#NextButton');
}
"""

SCAN_JS = PREAMBLE + r"""
() => {
  var out = {questions: [], validation: false, ended: false};
  var nb = nextBtn();
  out.hasNext = !!nb;
  out.nextVisible = nb ? vis(nb) : false;

  var verr = document.querySelectorAll('.ValidationError, .QuestionOuter .Error, [role=alert]');
  for (var i = 0; i < verr.length; i++) if (vis(verr[i])) out.validation = true;
  if (document.querySelector('#EndOfSurvey, .EndOfSurvey')) out.ended = true;

  var bs = blocks();
  for (var i = 0; i < bs.length; i++) {
    var b = bs[i], q = {index: i, text: qtext(b), type: null, options: []};
    var radios = Array.prototype.slice.call(b.querySelectorAll('input[type=radio]')).filter(vis);
    var checks = Array.prototype.slice.call(b.querySelectorAll('input[type=checkbox]')).filter(vis);
    var texts = Array.prototype.slice.call(
        b.querySelectorAll('input[type=text], input[type=number], textarea')).filter(vis);
    var sel = Array.prototype.slice.call(b.querySelectorAll('select')).filter(vis)[0];
    var sliders = b.querySelectorAll('input.RealSlider');
    var mrows = radios.length ? matrixRows(b) : [];

    if (sliders.length) {
      // The visible bar is chrome; the answer lives in a hidden range input.
      q.type = 'slider';
      q.count = sliders.length;
      q.min = Number(sliders[0].min || 0);
      q.max = Number(sliders[0].max || 10);
    } else if (mrows.length) {
      q.type = 'matrix';
      q.rows = mrows.map(function (r) { return r.label; });
      var heads = Array.prototype.slice.call(b.querySelectorAll('thead th, thead td'))
        .map(function (h) { return clean(h.innerText); }).filter(function (s) { return s; });
      var ncol = mrows[0].n;
      if (heads.length === ncol + 1) heads = heads.slice(1);
      q.options = heads.length === ncol ? heads
        : Array.apply(null, {length: ncol}).map(function (_, k) { return 'Option ' + (k + 1); });
    } else if (radios.length) {
      q.type = 'radio'; q.options = radios.map(labelFor);
    } else if (checks.length) {
      q.type = 'checkbox'; q.options = checks.map(labelFor);
    } else if (sel) {
      q.type = 'select';
      q.options = Array.prototype.slice.call(sel.querySelectorAll('option'))
        .filter(function (o) { return (o.value || '').trim(); })
        .map(function (o) { return clean(o.innerText); });
    } else if (texts.length) {
      q.type = 'text';
    } else {
      continue;
    }
    out.questions.push(q);
  }
  out.signature = out.questions.map(function (q) { return q.text.slice(0, 80); }).join('|')
                  + '||' + out.questions.length;
  return out;
}
"""

FILL_JS = PREAMBLE + r"""
(args) => {
  var b = blocks()[args.idx], p = args.payload;
  if (!b) return {ok: false, error: 'question gone'};

  if (p.type === 'slider') {
    var ss = Array.prototype.slice.call(b.querySelectorAll('input.RealSlider'));
    var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    ss.forEach(function (s, i) {
      var v = p.values[i] === undefined ? p.values[0] : p.values[i];
      setter.call(s, String(v));
      s.dispatchEvent(new Event('input', {bubbles: true}));
      s.dispatchEvent(new Event('change', {bubbles: true}));
    });
    return {ok: true};
  }
  if (p.type === 'matrix') {
    var rows = b.querySelectorAll('tr'), ri = 0;
    for (var i = 0; i < rows.length; i++) {
      var rs = Array.prototype.slice.call(rows[i].querySelectorAll('input[type=radio]')).filter(vis);
      if (rs.length < 2) continue;
      var c = p.cols[ri];
      if (c === null || c === undefined || c < 0 || c >= rs.length) c = 0;
      rs[c].click();
      ri++;
    }
    return {ok: true};
  }
  if (p.type === 'radio') {
    var rs = Array.prototype.slice.call(b.querySelectorAll('input[type=radio]')).filter(vis);
    rs[p.choice].click();
    return {ok: true};
  }
  if (p.type === 'checkbox') {
    var cs = Array.prototype.slice.call(b.querySelectorAll('input[type=checkbox]')).filter(vis);
    p.choices.forEach(function (c) { if (cs[c] && !cs[c].checked) cs[c].click(); });
    return {ok: true};
  }
  if (p.type === 'select') {
    var sel = Array.prototype.slice.call(b.querySelectorAll('select')).filter(vis)[0];
    var opts = Array.prototype.slice.call(sel.querySelectorAll('option'))
      .filter(function (o) { return (o.value || '').trim(); });
    sel.value = opts[p.choice].value;
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true};
  }
  if (p.type === 'text') {
    var t = Array.prototype.slice.call(
        b.querySelectorAll('input[type=text], input[type=number], textarea')).filter(vis)[0];
    if (!t) return {ok: false, error: 'no text field'};
    setValue(t, p.text);
    return {ok: true};
  }
  return {ok: false, error: 'unknown type ' + p.type};
}
"""


def scan(survey):
    return survey.evaluate(SCAN_JS)


def fill(survey, idx, payload):
    return survey.evaluate(FILL_JS, {"idx": idx, "payload": payload})


def click_next(survey):
    return survey.evaluate(PREAMBLE + "() => { var b = nextBtn();"
                           " if (!b || !vis(b)) return false; b.click(); return true; }")


def wait_for_content(page, timeout=40):
    """Qualtrics paints the frame before the block's content arrives, so a page
    that looks empty is usually just mid-render."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = scan(survey_frame(page))
        except PlaywrightError:
            page.wait_for_timeout(700)
            continue
        if state.get("questions") or state.get("ended"):
            return state
        page.wait_for_timeout(700)
    try:
        return scan(survey_frame(page))
    except PlaywrightError:
        return None


def wait_for_page_change(page, previous_signature, timeout=45):
    deadline = time.monotonic() + timeout
    last, stable = None, 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(800)
        try:
            state = scan(survey_frame(page))
        except PlaywrightError:
            continue
        if state.get("ended"):
            return state
        signature = state.get("signature", "")
        if signature == previous_signature:
            last, stable = None, 0
            continue
        if signature == last:
            stable += 1
            # An empty page needs longer to prove it is really empty.
            if stable >= (2 if state.get("questions") else 5):
                return state
        else:
            last, stable = signature, 1
    return None


# --------------------------------------------------------------------------
# The Emma Steel conversation
# --------------------------------------------------------------------------

# The survey's own custom JS is the source of truth for the conversation: the
# chat app posts its whole history to the parent, which stores it under
# `appData` and only then reveals the Next button. Reading that beats scraping
# the chat's DOM, and it is exactly what the study itself records.
READ_APPDATA_JS = r"""
() => {
  try {
    var raw = localStorage.getItem('appData');
    if (!raw) return null;
    function parseMaybe(value) {
      if (value == null) return null;
      if (typeof value === 'object') return value;
      if (typeof value !== 'string') return null;
      try { return JSON.parse(value); } catch (e) { return value; }
    }
    var obj = parseMaybe(raw);
    if (Array.isArray(obj)) obj = parseMaybe(obj.join(''));
    if (typeof obj === 'string') obj = parseMaybe(obj);
    if (!obj || typeof obj !== 'object') return {error: 'appData did not contain an object'};
    return {nextSection: !!obj.nextSection,
            messages: (obj.messages || []).map(function (m) {
              return {role: m.role, content: m.content};
            })};
  } catch (e) { return {error: String(e && e.message || e)}; }
}
"""


def read_app_data(survey):
    try:
        return survey.evaluate(READ_APPDATA_JS)
    except PlaywrightError:
        return None


def chat_elements(chat):
    """The chat app's own input and send button."""
    box = None
    for sel in ("textarea", "input[type='text']"):
        for el in chat.query_selector_all(sel):
            try:
                if el.is_visible() and el.is_enabled():
                    box = el
                    break
            except PlaywrightError:
                continue
        if box:
            break
    send = None
    for el in chat.query_selector_all("button, [role='button']"):
        try:
            if not el.is_visible():
                continue
            label = (el.inner_text() or "") + " " + (el.get_attribute("aria-label") or "")
            cls = el.get_attribute("class") or ""
            if re.search(r"send|submit|→|➤", label + " " + cls, re.I):
                send = el
                break
        except PlaywrightError:
            continue
    return box, send


def chat_visible_text(chat):
    try:
        return chat.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except PlaywrightError:
        return ""


def wait_for_bot_reply(page, chat, survey, before_count, before_text, sent_text, issues, turn):
    """Wait for Emma's next message. The message count in appData is the reliable
    signal; the visible text is the fallback while appData has not updated."""
    deadline = time.monotonic() + CHAT_REPLY_TIMEOUT_MS / 1000
    last, stable = None, 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(1200)

        data = read_app_data(survey)
        if data and data.get("messages") and len(data["messages"]) > before_count:
            tail = [m for m in data["messages"][before_count:]
                    if m.get("role") == "assistant" and m.get("content")]
            if tail:
                reply = tail[-1]["content"].strip()
                if reply == last:
                    stable += 1
                    if stable >= 2:
                        return reply, data
                else:
                    last, stable = reply, 1
                continue

        current = chat_visible_text(chat)
        reply = "\n".join(_strip_own_message(
            new_lines_since(before_text, current), sent_text)).strip()
        if not reply:
            continue
        if reply == last:
            stable += 1
            if stable >= 3:
                return reply, read_app_data(survey)
        else:
            last, stable = reply, 1

    if last:
        issues.append({"turn": turn, "kind": "slow_reply",
                       "detail": f"Emma's reply to turn {turn} was still arriving when the "
                                 f"{CHAT_REPLY_TIMEOUT_MS // 1000}s wait ran out; the text "
                                 f"recorded for that turn may be incomplete."})
        return last, read_app_data(survey)
    issues.append({"turn": turn, "kind": "no_reply",
                   "detail": f"Emma sent nothing within {CHAT_REPLY_TIMEOUT_MS // 1000}s "
                             f"of turn {turn}."})
    return None, read_app_data(survey)


def _norm_text(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def send_landed(chat, survey, sent_text, before_count, before_text, timeout=12):
    """Confirm the message left the box and is on screen.

    A click or an Enter press can be accepted by the page and still post
    nothing: the widget may re-render, the send button may be disabled while
    React catches up, or `fill()` may set a value the app never reads. The bot
    believes a turn was sent only once it can see it in the survey's appData
    transcript or in the chat's own visible text.
    """
    sent = _norm_text(sent_text)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page_wait = getattr(chat, "wait_for_timeout", None)
        if page_wait:
            try:
                page_wait(700)
            except PlaywrightError:
                time.sleep(0.7)
        else:
            time.sleep(0.7)

        data = read_app_data(survey)
        messages = (data or {}).get("messages") or []
        if len(messages) > before_count:
            for m in messages[before_count:]:
                if m.get("role") == "user" and sent and (
                        sent in _norm_text(m.get("content")) or
                        _norm_text(m.get("content")) in sent):
                    return True, data
            if any(m.get("role") == "assistant" for m in messages[before_count:]):
                return True, data

        current = chat_visible_text(chat)
        if sent and sent in _norm_text(current) and current != before_text:
            return True, data
    return False, None


def send_chat_message(chat, survey, box, send, text, before_count, before_text,
                      issues, turn):
    """Type a turn and keep trying until the message is actually on screen.

    Returns False rather than reporting a phantom send, so the log never
    contains a message the chatbot never received.
    """
    def type_text(replace):
        box.click(timeout=ACTION_TIMEOUT_MS)
        if replace:
            box.fill("", timeout=ACTION_TIMEOUT_MS)
        box.fill(text, timeout=ACTION_TIMEOUT_MS)

    def press_enter(_):
        box.press("Enter", timeout=ACTION_TIMEOUT_MS)

    def click_send(_):
        # Re-find the button: many chat widgets only enable (or render) it once
        # the box has text, so the handle taken before typing can be stale.
        _, fresh = chat_elements(chat)
        button = fresh or send
        if button is None:
            raise PlaywrightError("no send button")
        button.click(timeout=ACTION_TIMEOUT_MS)

    try:
        type_text(replace=False)
    except PlaywrightError as e:
        issues.append({"turn": turn, "kind": "input_failed",
                       "detail": f"Could not type turn {turn} into the chat box "
                                 f"({e.__class__.__name__})."})
        return False

    attempts = [("send button", click_send), ("Enter", press_enter)]
    if send is None:
        attempts.reverse()
    attempts.append(("a retype and Enter",
                     lambda _: (type_text(replace=True), press_enter(None))))

    last_error = None
    for label, action in attempts:
        try:
            action(None)
        except PlaywrightError as e:
            last_error = e
            continue
        landed, _ = send_landed(chat, survey, text, before_count, before_text)
        if landed:
            if label != attempts[0][0]:
                issues.append({"turn": turn, "kind": "send_retried",
                               "detail": f"Turn {turn} only went through after the bot "
                                         f"fell back to {label}; the first attempt left "
                                         f"the message sitting in the box."})
            return True
        print(f"  [chat] turn {turn}: {label} did not post the message, retrying")

    if last_error is not None:
        issues.append({"turn": turn, "kind": "send_failed",
                       "detail": f"Neither the send button nor Enter would send turn "
                                 f"{turn} ({last_error.__class__.__name__})."})
    else:
        issues.append({"turn": turn, "kind": "send_not_confirmed",
                       "detail": f"Turn {turn} was typed into the chat box but never "
                                 f"appeared in the conversation, so the bot stopped "
                                 f"rather than recording a message that was never sent."})
    return False


def run_conversation(page, survey, chat, client, model, persona, chat_log, issues, page_num):
    """Hold the conversation, up to MAX_CHAT_TURNS participant messages.

    Emma runs a scripted six-message arc (marked 1/6 ... 6/6) and then ends with
    a stop keyword, so a complete conversation fits inside the cap; the cap is
    there for the case where she does not stop."""
    print(f"  [chat] Emma Steel loaded on page {page_num}")

    opening = chat_visible_text(chat)
    data = read_app_data(survey)
    if data and data.get("messages"):
        for m in data["messages"]:
            if m.get("role") == "assistant" and m.get("content"):
                chat_log.append({"turn": 0, "speaker": "chatbot", "page": page_num,
                                 "text": m["content"], "words": word_count(m["content"])})
    elif _lines(opening):
        text = "\n".join(_lines(opening))
        chat_log.append({"turn": 0, "speaker": "chatbot", "page": page_num,
                         "text": text, "words": word_count(text)})

    last_bot = chat_log[-1]["text"] if chat_log else None
    if last_bot:
        print(f"  [chat] Emma opened: {last_bot[:90]!r}")

    used, finished = 0, False
    for turn in range(1, MAX_CHAT_TURNS + 1):
        box, send = chat_elements(chat)
        if box is None:
            if turn == 1:
                issues.append({"turn": turn, "kind": "no_chat_input",
                               "detail": "Emma's chat box was never found, so no "
                                         "conversation could be held."})
            else:
                print(f"  [chat] the chat box closed after {used} turn(s).")
            break

        text, grade = ask_chat_reply(client, model, persona, chat_log, last_bot, turn, issues)
        before_text = chat_visible_text(chat)
        data = read_app_data(survey)
        before_count = len(data["messages"]) if data and data.get("messages") else 0

        if not send_chat_message(chat, survey, box, send, text, before_count,
                                 before_text, issues, turn):
            break

        used = turn
        chat_log.append({"turn": turn, "speaker": "participant", "page": page_num,
                         "text": text, "words": word_count(text), "reading_grade": grade})
        print(f"  [chat] turn {turn} sent ({word_count(text)}w, grade {grade}): {text[:75]!r}")

        reply, data = wait_for_bot_reply(page, chat, survey, before_count, before_text,
                                         text, issues, turn)
        if reply is None:
            break
        chat_log.append({"turn": turn, "speaker": "chatbot", "page": page_num,
                         "text": reply, "words": word_count(reply)})
        print(f"  [chat] Emma replied ({word_count(reply)}w): {reply[:75]!r}")
        last_bot = reply

        if data and data.get("nextSection"):
            finished = True
            print(f"  [chat] the survey released the Next button after {used} turn(s).")
            break
        try:
            if scan(survey).get("nextVisible"):
                finished = True
                print(f"  [chat] Next appeared after {used} turn(s); the chat is done.")
                break
        except PlaywrightError:
            pass

    if used >= MAX_CHAT_TURNS and not finished:
        issues.append({"turn": MAX_CHAT_TURNS, "kind": "turn_cap",
                       "detail": f"The {MAX_CHAT_TURNS}-turn cap was reached before the survey "
                                 f"released the Next button, so the bot ended the conversation "
                                 f"rather than the study's own script doing it."})
        print(f"  [chat] hit the {MAX_CHAT_TURNS}-turn cap without the survey releasing Next.")
    return used, finished


# --------------------------------------------------------------------------
# Answering ordinary survey questions
# --------------------------------------------------------------------------

def answer_questions(survey, state, client, model, persona, history, transcript,
                     flags, q_counter, page_num, issues):
    for q in state["questions"]:
        q_counter[0] += 1
        idx = q_counter[0]
        entry = {"index": idx, "page": page_num, "question": q["text"],
                 "type": q["type"], "answer": None}

        options = q.get("options") or None
        extra = None
        llm_type = q["type"]
        if q["type"] == "matrix":
            extra = {"rows": q.get("rows", []), "cols": q.get("options", [])}
        elif q["type"] == "slider":
            # Offer the scale points as choices so the persona picks a number.
            lo, hi = int(q.get("min", 0)), int(q.get("max", 10))
            options = [str(v) for v in range(lo, hi + 1)]
            llm_type = "radio"

        enriched = dict(persona)
        enriched["personality"] = (
            f"{persona_block(persona)}\n\n{opinion_block(persona)}\n\n"
            f"Write free text the way this person really talks: plain words, no jargon, "
            f"no bullet points, at most {MAX_REPLY_WORDS} words.")

        result = base_ask_llm(client, model, enriched, history, q["text"],
                              llm_type, options, extra) or {}

        try:
            if q["type"] == "slider":
                lo, hi = int(q.get("min", 0)), int(q.get("max", 10))
                c = result.get("choice_index")
                value = lo + c if isinstance(c, int) and 0 <= c <= hi - lo else (lo + hi) // 2
                fill(survey, q["index"],
                     {"type": "slider", "values": [value] * max(q.get("count", 1), 1)})
                entry["answer"] = value
            elif q["type"] == "matrix":
                rows = q.get("rows", [])
                picks = result.get("row_choices") or {}
                ncol = max(len(q.get("options", [])), 1)
                cols = []
                for label in rows:
                    c = picks.get(label)
                    cols.append(c if isinstance(c, int) and 0 <= c < ncol
                                else random.randrange(ncol))
                fill(survey, q["index"], {"type": "matrix", "cols": cols})
                entry["answer"] = {r: (q["options"][c] if c < len(q["options"]) else c)
                                   for r, c in zip(rows, cols)}
            elif q["type"] in ("radio", "select"):
                c = result.get("choice_index")
                if not (isinstance(c, int) and 0 <= c < len(q["options"])):
                    c = random.randrange(len(q["options"]))
                fill(survey, q["index"], {"type": q["type"], "choice": c})
                entry["answer"] = q["options"][c]
            elif q["type"] == "checkbox":
                picks = [c for c in (result.get("choice_indices") or [])
                         if isinstance(c, int) and 0 <= c < len(q["options"])]
                if not picks:
                    picks = [random.randrange(len(q["options"]))]
                fill(survey, q["index"], {"type": "checkbox", "choices": picks})
                entry["answer"] = [q["options"][c] for c in picks]
            else:
                if is_opening_opinion_question(q["text"]) and persona.get("opinion_statement"):
                    text = persona["opinion_statement"]
                else:
                    text = result.get("text") or "I'm not really sure how to say it."
                if word_count(text) > MAX_REPLY_WORDS:
                    text = trim_to_words(text, MAX_REPLY_WORDS)
                fill(survey, q["index"], {"type": "text", "text": text})
                entry["answer"] = text
        except PlaywrightError as e:
            entry["error"] = f"{e.__class__.__name__}"
            issues.append({"turn": 0, "kind": "fill_failed",
                           "detail": f"Page {page_num}: could not fill "
                                     f"{q['text'][:60]!r} ({e.__class__.__name__})."})

        if result.get("fitting") is False:
            flags.append({"index": idx, "question": q["text"],
                          "reason": result.get("reason", "")})
        entry["fitting"] = result.get("fitting")
        entry["fit_reason"] = result.get("reason", "")
        history.append({"question": q["text"], "answer": entry["answer"]})
        transcript.append(entry)
        print(f"  Q{idx} ({q['type']}): {q['text'][:58]!r} -> {str(entry['answer'])[:60]!r}")


# --------------------------------------------------------------------------
# One pass through the survey
# --------------------------------------------------------------------------

def _save_stop_shot(page, run_number, survey_page, out_dir):
    if out_dir is None:
        return
    path = Path(out_dir) / f"chat_run_{run_number}_stopped_page{survey_page}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"[Run {run_number}] Screenshot of the stopping point: {path}")
    except PlaywrightError:
        pass


def run_once(context, preview_url, client, model, persona, run_number,
             max_pages=200, out_dir=None):
    history, transcript, flags, chat_log, issues = [], [], [], [], []
    q_counter = [0]
    chat_pages = []
    pending_no_chat = {}
    survey_page = 1
    last_signature, stuck = None, 0

    print(f"\n=== Run {run_number}: {persona.get('name')}, {persona.get('education')} ===")
    print(f"    Opinion: {persona.get('opinion_statement', '')[:95]}...")

    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    try:
        page.goto(preview_url, wait_until="domcontentloaded")
        state = wait_for_content(page)
        if not state or not (state.get("questions") or state.get("ended")):
            issues.append({"turn": 0, "kind": "no_survey",
                           "detail": "The survey never rendered after the preview link "
                                     "was opened."})
            print(f"[Run {run_number}] The survey did not load.")
            return transcript, chat_log, flags, issues, chat_pages

        for _ in range(max_pages):
            survey = survey_frame(page)
            if state.get("ended"):
                print(f"[Run {run_number}] Reached the end after {survey_page - 1} pages.")
                break

            signature = state.get("signature", "")
            if signature and signature == last_signature:
                stuck += 1
                if stuck >= 3:
                    issues.append({"turn": 0, "kind": "stuck_page",
                                   "detail": f"The survey would not advance past page "
                                             f"{survey_page}."})
                    print(f"[Run {run_number}] Stuck on page {survey_page}. Stopping.")
                    _save_stop_shot(page, run_number, survey_page, out_dir)
                    break
            else:
                last_signature, stuck = signature, 0

            chat = chat_frame(page, survey)
            if chat is None and not state.get("questions"):
                print(f"[Run {run_number}] Page {survey_page}: waiting for chatbot frame...")
                chat = wait_for_chat_frame(page, survey)
            if chat is not None:
                chat_pages.append(survey_page)
                pending_no_chat.pop(survey_page, None)
                # The chat frame loads its own app; give it a moment to paint.
                page.wait_for_timeout(3000)
                run_conversation(page, survey, chat, client, model, persona,
                                 chat_log, issues, survey_page)
            elif state.get("questions"):
                answer_questions(survey, state, client, model, persona, history,
                                 transcript, flags, q_counter, survey_page, issues)
            else:
                # Held back rather than reported here: a page that does not
                # advance is visited again, and the chat frame often shows up on
                # that second look. Reporting at once is what produced "no chat
                # widget was found" for a page the bot then chatted on.
                pending_no_chat[survey_page] = {
                    "turn": 0, "kind": "no_chat_found",
                    "detail": f"Page {survey_page} had no ordinary survey questions, and "
                              f"no chatbot frame was found after waiting. The page had "
                              f"{len(page.frames)} frame(s) and chatParams "
                              f"{'was' if _chat_params_app_url(page, survey) else 'was not'} "
                              f"set."}
                print(f"[Run {run_number}] Page {survey_page}: nothing to answer.")

            if not click_next(survey):
                if state.get("ended"):
                    break
                print(f"[Run {run_number}] No Next button on page {survey_page}. Stopping.")
                _save_stop_shot(page, run_number, survey_page, out_dir)
                break

            moved = wait_for_page_change(page, signature)
            if moved is None:
                print(f"[Run {run_number}] Page {survey_page} did not change after Next.")
                state = wait_for_content(page, timeout=15) or state
                continue
            state = moved
            survey_page += 1
        else:
            print(f"[Run {run_number}] Hit the max_pages limit ({max_pages}).")
    finally:
        try:
            page.close()
        except PlaywrightError:
            pass

    for chat_page in sorted(pending_no_chat):
        if chat_page not in chat_pages:
            issues.append(pending_no_chat[chat_page])

    return transcript, chat_log, flags, issues, chat_pages


def _write_run_log(out_dir, run_number, persona, transcript, chat_log, flags,
                   issues, chat_pages):
    """One run's complete record: what it answered, what was said, what went wrong."""
    if not chat_pages:
        issues.append({"turn": 0, "kind": "no_chat_found",
                       "detail": "No chat frame was found on any page of this run, so no "
                                 "conversation took place."})

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = Path(out_dir) / f"chat_run_{run_number}_{timestamp}.json"
    with open(out_file, "w") as f:
        json.dump({"run": run_number, "persona": persona,
                   "opinion_statement": persona.get("opinion_statement", ""),
                   "transcript": transcript, "chat": chat_log,
                   "flags": flags, "chat_issues": issues,
                   "chat_pages": chat_pages}, f, indent=2, ensure_ascii=False)

    participant_turns = sum(1 for m in chat_log if m["speaker"] == "participant")
    print(f"[Run {run_number}] Saved {out_file.name}: {len(transcript)} survey answer(s), "
          f"{participant_turns} chat turn(s), {len(issues)} issue(s).")
    return out_file


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Preview bot that completes a Qualtrics political-discussion survey "
                    "and its embedded chatbot conversation.")
    parser.add_argument("preview_url", help="The Qualtrics preview link")
    parser.add_argument("--runs", type=int, default=1,
                        help="Passes through the survey (the study calls for 10-15)")
    parser.add_argument("--headless", action="store_true", help="No visible browser window")
    parser.add_argument("--persona", default="persona_hs_political.json")
    parser.add_argument("--opinions", default=OPINION_POOL_FILE,
                        help="JSON list of opinion statements, used verbatim")
    parser.add_argument("--model", default="gpt-4o-mini", help="OpenAI model for the persona")
    parser.add_argument("--slow", type=int, default=100, help="Delay per browser action (ms)")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR,
                        help="Browser profile folder that holds the Qualtrics session")
    parser.add_argument("--engine", default="webkit",
                        choices=["webkit", "firefox", "chromium"],
                        help="Browser engine. WebKit is Safari's engine and the only one "
                             "some institutions' logins complete in; Chromium loops on the "
                             "datacenter redirect.")
    parser.add_argument("--browser-path", default=None,
                        help="Browser binary, for --engine chromium only")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed the opinion draw so a batch can be reproduced")
    args = parser.parse_args()

    if "/preview/" not in args.preview_url:
        print("Warning: that URL does not look like a Qualtrics PREVIEW link. "
              "Only run this against previews.")

    if args.seed is not None:
        random.seed(args.seed)

    browser_path = find_browser(args.browser_path) if args.engine == "chromium" else None
    print(f"Engine: {args.engine}{f' ({browser_path})' if browser_path else ''}")

    client = OpenAI()
    base_persona = load_persona(args.persona)
    pool = load_opinion_pool(args.opinions)
    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    log_files = []
    with sync_playwright() as p:
        context = open_browser(p, args.profile_dir, browser_path, args.headless,
                               args.slow, args.engine)
        try:
            gate = context.new_page()
            gate.set_default_timeout(ACTION_TIMEOUT_MS)
            if not wait_for_preview_access(gate, args.preview_url):
                print("Could not reach the preview. Nothing was run.")
                sys.exit(1)
            try:
                gate.close()
            except PlaywrightError:
                pass

            for i in range(1, args.runs + 1):
                persona = dict(base_persona)
                persona["opinion_statement"] = random.choice(pool)
                transcript, chat_log, flags, issues, chat_pages = run_once(
                    context, args.preview_url, client, args.model, persona, i,
                    args.max_pages, out_dir)
                log_files.append(_write_run_log(out_dir, i, persona, transcript,
                                                chat_log, flags, issues, chat_pages))
        finally:
            context.close()

    if log_files:
        xlsx = out_dir / "qualtrics_chat_transcript.xlsx"
        export_chat_to_excel.build_workbook([str(p) for p in log_files], str(xlsx))
        md = out_dir / "CHAT_CHALLENGES.md"
        write_chat_challenges.write_report([str(p) for p in log_files], str(md))
        print(f"\nWorkbook: {xlsx}\nChallenges report: {md}")


if __name__ == "__main__":
    main()
