#!/usr/bin/env python3
"""
Qualtrics Preview Agent (LLM-driven)
-------------------------------------
Automates clicking through a Qualtrics SURVEY PREVIEW like a human
respondent would: it maintains a consistent persona, writes plausible
answers (including free text) with GPT, and after each question judges
whether the survey's next question is a sensible/appropriate follow-up
given everything answered so far (catches bad branching logic, e.g. a
pregnancy question shown to a respondent who said "male").

WHY PREVIEW ONLY: Qualtrics preview mode does not record real responses,
so this is safe to run repeatedly. Never point this at a live/anonymous
distribution link meant for real respondents.

SETUP (run once):
    pip install playwright openai
    playwright install chromium
    export OPENAI_API_KEY="sk-..."

USAGE:
    python qualtrics_preview_agent.py "https://<your-brand>.qualtrics.com/jfe/preview/previewId/<id>/SV_<survey id>?Q_CHL=preview" --runs 3

    --runs N          Number of times to run through the survey
    --headless        Run without showing the browser window
    --persona FILE    Optional JSON file describing the respondent persona
                       (see PERSONA FORMAT below). If omitted, GPT invents
                       a new persona each run for variety.
    --model NAME       OpenAI model to use (default: gpt-4o-mini)
    --slow MS          Slow down each browser action by MS ms (default 150)
    --max-pages N      Safety cap on pages per run (default 200)
    --out-dir DIR      Where to write the JSON logs (default: cwd)

PERSONA FORMAT (optional, --persona persona.json):
    {
      "name": "Jordan",
      "age": 29,
      "gender": "female",
      "occupation": "graduate student",
      "personality": "thoughtful, a bit skeptical of surveys, concise answers"
    }

OUTPUT:
    - Console transcript as it runs, including any flagged questions.
    - qualtrics_run_<n>_<timestamp>.json: full Q&A log with the persona
      used, every question/answer (with question type and the LLM's
      fit judgement), and a separate "flags" list of any questions the
      LLM judged as not fitting the conversation so far.
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


NEXT_BUTTON_SELECTORS = [
    "#NextButton",
    "input#NextButton",
    "button:has-text('Next')",
    "input[value='Next']",
    "button[type='submit']",
]

# Only structural, end-of-survey-specific markers. Free text like
# "Thank you" also appears mid-survey and inside hidden blocks, so it is
# never used on its own to decide the survey is over.
END_OF_SURVEY_SELECTORS = [
    "#EndOfSurvey",
    ".EndOfSurvey",
    "#SkinContent .end-of-survey",
]

VALIDATION_SELECTORS = [
    ".ValidationError",
    ".QuestionOuter .Error",
    "[role='alert']",
    "text=requires an answer",
    "text=Please answer this question",
]

# Element-level actions get a short timeout: a 30s default turns one
# mis-detected field into a stalled run.
ACTION_TIMEOUT_MS = 5000
PAGE_SETTLE_TIMEOUT_MS = 15000
POLL_INTERVAL_MS = 300


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _as_int(value):
    """LLMs happily return "2" where an int was asked for."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+", value)
        if m:
            return int(m.group())
    return None


def _visible(el):
    try:
        return el.is_visible()
    except PlaywrightError:
        return False


def _parse_json_object(raw):
    """Tolerate ```json fences and stray prose around the object."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _chat_json(client, model, prompt, temperature, retries=2):
    """One-shot JSON chat call with retries; returns a dict or None."""
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                response_format={"type": "json_object"},
            )
        except Exception as e:  # network hiccup, rate limit, bad model name
            if attempt == retries:
                print(f"  ! OpenAI call failed ({e.__class__.__name__}: {e}); falling back.")
                return None
            time.sleep(1.5 * (attempt + 1))
            continue
        parsed = _parse_json_object(resp.choices[0].message.content)
        if parsed is not None:
            return parsed
        if attempt == retries:
            print("  ! Could not parse LLM JSON output; falling back.")
    return None


# --------------------------------------------------------------------------
# Persona
# --------------------------------------------------------------------------

DEFAULT_PERSONA = {
    "name": "Alex",
    "age": 34,
    "gender": "non-binary",
    "occupation": "office administrator",
    "personality": "cooperative, gives short plain answers",
}


def load_persona(path):
    if not path:
        return None
    with open(path, "r") as f:
        persona = json.load(f)
    if not isinstance(persona, dict):
        raise ValueError(f"{path}: persona file must contain a JSON object")
    return persona


def generate_persona(client, model):
    prompt = (
        "Invent one realistic, ordinary survey respondent persona for testing "
        "an online survey. Return ONLY a JSON object with keys: name, age, "
        "gender, occupation, personality (a short phrase describing their "
        "tone/attitude when answering surveys, e.g. 'chatty, opinionated' or "
        "'terse, only answers what's asked'). No preamble, no markdown."
    )
    persona = _chat_json(client, model, prompt, temperature=1.0)
    if not isinstance(persona, dict) or not persona.get("name"):
        print("  ! Persona generation failed; using the built-in default persona.")
        return dict(DEFAULT_PERSONA)
    return persona


# --------------------------------------------------------------------------
# LLM answer + appropriateness judgment
# --------------------------------------------------------------------------

def ask_llm(client, model, persona, history, question_text, q_type, options, extra=None):
    """
    Returns a dict:
      {
        "choice_index": int or None,       # for radio / select
        "choice_indices": [int,...] or None,  # for checkbox
        "text": str or None,               # for text entry
        "row_choices": {row_label: col_index, ...} or None,  # for matrix
        "fitting": bool,
        "reason": str
      }
    """
    persona_desc = (
        f"You are role-playing as a survey respondent with this persona: "
        f"name={persona.get('name')}, age={persona.get('age')}, "
        f"gender={persona.get('gender')}, occupation={persona.get('occupation')}, "
        f"personality={persona.get('personality')}. "
        f"Answer as this person would, consistently with everything you've "
        f"answered before in this survey."
    )

    history_text = "\n".join(
        f"- Q: {h['question']}\n  A: {h['answer']}" for h in history[-10:]
    ) or "(no prior questions yet)"

    schema_hint = {
        "radio": "Return {\"choice_index\": <int index into options>, \"fitting\": <bool>, \"reason\": \"<short>\"}",
        "checkbox": "Return {\"choice_indices\": [<int indices into options>], \"fitting\": <bool>, \"reason\": \"<short>\"}",
        "select": "Return {\"choice_index\": <int index into options>, \"fitting\": <bool>, \"reason\": \"<short>\"}",
        "text": "Return {\"text\": \"<your written answer, 1-3 sentences unless the question calls for a short answer>\", \"fitting\": <bool>, \"reason\": \"<short>\"}",
        "matrix": "Return {\"row_choices\": {\"<row label>\": <int index into options (columns)>, ...}, \"fitting\": <bool>, \"reason\": \"<short>\"}",
    }[q_type]

    options_text = ""
    if q_type == "matrix":
        rows, cols = extra["rows"], extra["cols"]
        options_text = f"Columns (choose one index per row): {json.dumps(cols)}\nRows: {json.dumps(rows)}"
    elif options is not None:
        options_text = f"Options (choose by index): {json.dumps(options)}"

    prompt = (
        f"{persona_desc}\n\n"
        f"Conversation so far (questions already answered in this survey):\n{history_text}\n\n"
        f"Current question ({q_type}): {question_text}\n{options_text}\n\n"
        f"Two tasks:\n"
        f"1. Answer as the persona would, staying consistent with prior answers.\n"
        f"2. Judge whether THIS question is a sensible, appropriate follow-up "
        f"given everything answered so far (e.g. flag it if it contradicts "
        f"earlier answers, asks something already answered, or seems like a "
        f"branching logic error). Set fitting=false only for real problems, "
        f"not stylistic nitpicks.\n\n"
        f"{schema_hint}\nReturn ONLY the JSON object, no markdown, no preamble."
    )

    result = _chat_json(client, model, prompt, temperature=0.8)
    if result is None:
        return {"fitting": True, "reason": "(LLM unavailable; answered at random)",
                "text": "Test response", "llm_failed": True}
    return result


# --------------------------------------------------------------------------
# Locating the survey itself
# --------------------------------------------------------------------------

# The jfe5 preview wraps the real survey in an iframe and renders a second,
# mirrored copy for the mobile view; answering both would answer every
# question twice. Classic /jfe/ links render the survey in the page itself.
SURVEY_FRAME_NAME = "preview-view"
SURVEY_FRAME_URL_RE = re.compile(r"/jfe/(previewForm|form)/")


def survey_surface(page):
    """Return the Page or Frame that actually holds the survey. Frame and Page
    expose the same query/locator API, so callers treat them alike."""
    for fr in page.frames:
        if fr.name == SURVEY_FRAME_NAME:
            return fr
    candidates = [fr for fr in page.frames
                  if fr is not page.main_frame and SURVEY_FRAME_URL_RE.search(fr.url or "")]
    for fr in candidates:
        if "mobile" not in (fr.name or ""):
            return fr
    if candidates:
        return candidates[0]
    return page.main_frame


# --------------------------------------------------------------------------
# Page interaction
# --------------------------------------------------------------------------

LABEL_JS = """
el => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  if (el.id) {
    for (const l of document.querySelectorAll('label')) {
      if (l.htmlFor === el.id) {
        const t = clean(l.innerText || l.textContent);
        if (t) return t;
      }
    }
  }
  const wrapping = el.closest('label');
  if (wrapping) {
    const t = clean(wrapping.innerText || wrapping.textContent);
    if (t) return t;
  }
  const aria = el.getAttribute('aria-label');
  if (clean(aria)) return clean(aria);
  const labelledby = el.getAttribute('aria-labelledby');
  if (labelledby) {
    const parts = labelledby.split(/\\s+/)
      .map(id => document.getElementById(id))
      .filter(Boolean)
      .map(n => clean(n.innerText || n.textContent))
      .filter(Boolean);
    if (parts.length) return parts.join(' ');
  }
  const li = el.closest('li, td, .Selection, .q-checkbox-container, .q-radio-container');
  if (li) {
    const t = clean(li.innerText || li.textContent);
    if (t) return t;
  }
  return '';
}
"""


def _label_for_input(input_el):
    """Human-readable choice text; the value attribute is a Qualtrics choice ID."""
    try:
        text = input_el.evaluate(LABEL_JS)
    except PlaywrightError:
        text = ""
    if text:
        return text
    return input_el.get_attribute("value") or "(unlabeled)"


def get_column_headers(block, n_cols):
    headers = block.query_selector_all("thead th, thead td")
    labels = []
    for h in headers:
        text = h.inner_text().strip()
        if text:
            labels.append(text)
    if len(labels) == n_cols:
        return labels
    # Qualtrics matrices usually carry a leading blank header for the row labels.
    if len(labels) == n_cols + 1:
        return labels[1:]
    return [f"Option {i + 1}" for i in range(n_cols)]


def visible_question_blocks(surface):
    """Qualtrics leaves display-logic-hidden questions in the DOM; skip them."""
    return [b for b in surface.query_selector_all(".QuestionOuter") if _visible(b)]


def _matrix_rows(block):
    """Return [(row_label, [radio elements])] if this block is a matrix table."""
    rows = block.query_selector_all("tr")
    found, names = [], set()
    for row in rows:
        row_radios = [r for r in row.query_selector_all("input[type='radio']") if _visible(r)]
        if len(row_radios) < 2:
            continue
        for r in row_radios:
            names.add(r.get_attribute("name") or "")
        label_el = row.query_selector("th, td:first-child, .RowLabel")
        label = label_el.inner_text().strip() if label_el else ""
        found.append((label or f"row{len(found) + 1}", row_radios))
    # A single radio group laid out across one row is not a matrix.
    if len(found) >= 2 and len(names) >= 2:
        return found
    return []


def _question_text(block, idx):
    text_el = block.query_selector(".QuestionText")
    if text_el:
        text = text_el.inner_text().strip()
        if text:
            return text
    text = block.inner_text().strip()
    return text.split("\n")[0] if text else f"(question {idx}, text not found)"


def answer_page(surface, client, model, persona, history, transcript, flags, q_counter, page_num=0):
    answered_any = False
    question_blocks = visible_question_blocks(surface)
    if not question_blocks:
        return False

    for block in question_blocks:
        q_counter[0] += 1
        idx = q_counter[0]
        q_text = _question_text(block, idx)

        entry = {"index": idx, "page": page_num, "question": q_text,
                 "answer": None, "type": None}
        result = None

        radios = [r for r in block.query_selector_all("input[type='radio']") if _visible(r)]
        checkboxes = [c for c in block.query_selector_all("input[type='checkbox']") if _visible(c)]
        text_field = None
        for t in block.query_selector_all("input[type='text'], input[type='number'], textarea"):
            if _visible(t):
                text_field = t
                break
        select_el = None
        for s in block.query_selector_all("select"):
            if _visible(s):
                select_el = s
                break
        matrix_rows = _matrix_rows(block) if radios else []

        try:
            # Matrix first: its cells are radios too, so the radio branch would
            # otherwise swallow it and answer a single cell of the grid.
            if matrix_rows:
                entry["type"] = "matrix"
                row_labels = [label for label, _ in matrix_rows]
                row_els = [els for _, els in matrix_rows]
                n_cols = max(len(els) for els in row_els)
                cols = get_column_headers(block, n_cols)
                result = ask_llm(client, model, persona, history, q_text, "matrix", None,
                                 extra={"rows": row_labels, "cols": cols})
                row_choices = result.get("row_choices") or {}
                final = {}
                for label, radios_in_row in zip(row_labels, row_els):
                    col_idx = _as_int(row_choices.get(label))
                    if col_idx is None or not (0 <= col_idx < len(radios_in_row)):
                        col_idx = random.randrange(len(radios_in_row))
                    _check(radios_in_row[col_idx])
                    final[label] = cols[col_idx] if col_idx < len(cols) else col_idx
                entry["answer"] = final

            elif radios:
                entry["type"] = "radio"
                options = [_label_for_input(r) for r in radios]
                result = ask_llm(client, model, persona, history, q_text, "radio", options)
                idx_choice = _as_int(result.get("choice_index"))
                if idx_choice is None or not (0 <= idx_choice < len(radios)):
                    idx_choice = random.randrange(len(radios))
                _check(radios[idx_choice])
                entry["answer"] = options[idx_choice]

            elif checkboxes:
                entry["type"] = "checkbox"
                options = [_label_for_input(b) for b in checkboxes]
                result = ask_llm(client, model, persona, history, q_text, "checkbox", options)
                raw_picks = result.get("choice_indices") or []
                if not isinstance(raw_picks, list):
                    raw_picks = [raw_picks]
                picks, seen = [], set()
                for p in raw_picks:
                    i = _as_int(p)
                    if i is not None and 0 <= i < len(checkboxes) and i not in seen:
                        seen.add(i)
                        picks.append(i)
                if not picks:
                    picks = [random.randrange(len(checkboxes))]
                for i in picks:
                    _check(checkboxes[i])
                entry["answer"] = [options[i] for i in picks]

            elif select_el:
                entry["type"] = "select"
                option_els = [o for o in select_el.query_selector_all("option")
                              if (o.get_attribute("value") or "").strip()]
                options = [o.inner_text().strip() for o in option_els]
                if not option_els:
                    entry["answer"] = None
                else:
                    result = ask_llm(client, model, persona, history, q_text, "select", options)
                    idx_choice = _as_int(result.get("choice_index"))
                    if idx_choice is None or not (0 <= idx_choice < len(option_els)):
                        idx_choice = random.randrange(len(option_els))
                    select_el.select_option(value=option_els[idx_choice].get_attribute("value"),
                                            timeout=ACTION_TIMEOUT_MS)
                    entry["answer"] = options[idx_choice]

            elif text_field:
                entry["type"] = "text"
                result = ask_llm(client, model, persona, history, q_text, "text", None)
                value = result.get("text") or "Test response"
                if not isinstance(value, str):
                    value = json.dumps(value)
                text_field.fill(value, timeout=ACTION_TIMEOUT_MS)
                entry["answer"] = value

            else:
                # Descriptive text / graphic block, nothing to answer.
                q_counter[0] -= 1
                continue

        except PlaywrightError as e:
            entry["error"] = f"{e.__class__.__name__}: {str(e).splitlines()[0]}"
            print(f"  ! Q{idx}: could not fill in ({entry['error']})")

        if result:
            fitting = bool(result.get("fitting", True))
            reason = result.get("reason", "")
            entry["fitting"] = fitting
            entry["fit_reason"] = reason
            if result.get("llm_failed"):
                entry["llm_failed"] = True
            if not fitting:
                flags.append({"index": idx, "question": q_text, "reason": reason})
                print(f"  ⚠ FLAGGED (Q{idx}): {reason}")

        history.append({"question": q_text, "answer": entry["answer"]})
        transcript.append(entry)
        answered_any = True
        print(f"  Q{idx} ({entry['type']}): {q_text[:70]!r} -> {entry['answer']}")

    return answered_any


def _check(input_el):
    """Qualtrics hides the real input behind a styled label, so click the label
    when a plain check is not actionable."""
    try:
        input_el.check(timeout=ACTION_TIMEOUT_MS)
        return
    except PlaywrightError:
        pass
    try:
        input_el.check(force=True, timeout=ACTION_TIMEOUT_MS)
    except PlaywrightError:
        input_el.evaluate(
            "el => { el.checked = true;"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true})); }"
        )


def page_signature(surface):
    """Identifies the page so we can tell an advance from a stuck page."""
    parts = []
    for b in visible_question_blocks(surface):
        parts.append(_question_text(b, 0)[:120])
    return f"{surface.url}||{len(parts)}||" + "|".join(parts)


def has_validation_error(surface):
    for sel in VALIDATION_SELECTORS:
        try:
            loc = surface.locator(sel)
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return True
        except PlaywrightError:
            continue
    return False


def click_next(surface):
    for sel in NEXT_BUTTON_SELECTORS:
        for btn in surface.query_selector_all(sel):
            if not _visible(btn):
                continue
            try:
                if btn.is_disabled():
                    continue
            except PlaywrightError:
                pass
            try:
                btn.click(timeout=ACTION_TIMEOUT_MS)
                return True
            except PlaywrightError:
                continue
    return False


def wait_for_page_change(page, previous_signature, timeout_ms=PAGE_SETTLE_TIMEOUT_MS):
    """Qualtrics swaps pages in-place via JS, so there is no navigation event to
    await; poll until the page both differs from the old one AND stops changing.
    Without the stability check the agent sees the momentarily-empty page
    mid-transition and clicks Next again, skipping a whole survey page.

    Takes the top-level Page and re-resolves the survey frame on every poll,
    because Qualtrics can swap the iframe out mid-transition."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_signature = None
    stable_polls = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(POLL_INTERVAL_MS)
        try:
            surface = survey_surface(page)
            if at_end_of_survey(surface):
                return "end"
            if has_validation_error(surface):
                return "validation"
            signature = page_signature(surface)
        except PlaywrightError:
            continue  # frame detached mid-swap; look again next poll
        if signature == previous_signature:
            last_signature, stable_polls = None, 0
            continue
        if signature == last_signature:
            stable_polls += 1
        else:
            last_signature, stable_polls = signature, 1
        # A page with no questions may just be mid-render, so give it longer to
        # settle than one that already shows content.
        needed = 2 if visible_question_blocks(surface) else 4
        if stable_polls >= needed:
            return "changed"
    return "timeout"


def at_end_of_survey(surface):
    for sel in END_OF_SURVEY_SELECTORS:
        try:
            loc = surface.locator(sel)
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return True
        except PlaywrightError:
            continue
    # No questions left and no way forward: treat a visible thank-you as the end.
    if not visible_question_blocks(surface):
        try:
            thanks = surface.get_by_text(re.compile(r"thank you|survey is (now )?closed|already been completed", re.I))
            if thanks.count() and thanks.first.is_visible() and not _has_next_button(surface):
                return True
        except PlaywrightError:
            pass
    return False


def _has_next_button(surface):
    for sel in NEXT_BUTTON_SELECTORS:
        for btn in surface.query_selector_all(sel):
            if _visible(btn):
                return True
    return False


# --------------------------------------------------------------------------
# Qualtrics login session
# --------------------------------------------------------------------------

def at_login_wall(page):
    """Preview links are account-protected: a fresh browser profile gets
    bounced to /login instead of the survey."""
    return "/login" in page.url and "qualtrics.com" in page.url


def _logged_in_page(context):
    """Any tab that is on Qualtrics but no longer on the login page. Qualtrics
    often opens the preview in a second tab, so check every page in the context."""
    for pg in context.pages:
        try:
            url = pg.url
        except PlaywrightError:
            continue
        if "qualtrics.com" in url and "/login" not in url and not url.startswith("about:"):
            return pg
    return None


def save_login_state(preview_url, auth_file, slow_ms):
    """Open a visible browser, let the human log into Qualtrics once, then
    store the session cookies so later (headless) runs reuse them. Only the
    cookies matter, so this waits to be off the login page rather than trying
    to recognise survey markup, which differs between Qualtrics players."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=slow_ms)
        context = browser.new_context()
        page = context.new_page()
        page.goto(preview_url, wait_until="domcontentloaded")
        print("A browser window is open. Log into Qualtrics there (SSO included).")
        print("Waiting for the login to go through; the window closes by itself.")
        deadline = time.monotonic() + 600
        stable = 0
        last_note = 0.0
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            if _logged_in_page(context) is not None:
                stable += 1
            else:
                stable = 0
            if stable >= 3:
                page.wait_for_timeout(2000)
                context.storage_state(path=auth_file)
                open_tabs = [pg.url for pg in context.pages]
                browser.close()
                print(f"Session saved to {auth_file}.")
                print("Tabs seen at save time:")
                for url in open_tabs:
                    print(f"  {url[:120]}")
                print("Re-run without --login.")
                return True
            now = time.monotonic()
            if now - last_note > 15:
                last_note = now
                print(f"  ...still on the login page ({int(deadline - now)}s left). "
                      f"Close this with Ctrl+C if you are already logged in and it is stuck.")
        print("Timed out after 10 minutes waiting for login. Nothing saved.")
        browser.close()
        return False


# --------------------------------------------------------------------------
# Run loop
# --------------------------------------------------------------------------

def _drop_page_entries(transcript, history, q_counter, survey_page):
    """A page we are about to re-answer must not leave duplicate entries behind."""
    doomed = [e for e in transcript if e.get("page") == survey_page]
    for e in doomed:
        transcript.remove(e)
    if doomed:
        del history[-len(doomed):]
        q_counter[0] -= len(doomed)


def _save_stop_shot(page, run_number, survey_page, out_dir):
    """A run that stops early is nearly impossible to diagnose from text alone."""
    if out_dir is None:
        return
    path = Path(out_dir) / f"qualtrics_run_{run_number}_stopped_page{survey_page}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"[Run {run_number}] Screenshot of the stopping point: {path}")
    except PlaywrightError as e:
        print(f"[Run {run_number}] Could not screenshot the stopping point: {e.__class__.__name__}")


def run_once(preview_url, client, model, persona, headless, slow_ms, run_number,
             max_pages=200, auth_file=None, out_dir=None):
    history = []
    transcript = []
    flags = []
    q_counter = [0]
    stuck_signature = None
    stuck_count = 0
    survey_page = 1          # only advances when the survey actually advances
    retrying = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_ms)
        try:
            storage = auth_file if (auth_file and Path(auth_file).exists()) else None
            context = browser.new_context(storage_state=storage)
            page = context.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            print(f"\n=== Run {run_number}: persona = {persona.get('name')}, "
                  f"{persona.get('age')}, {persona.get('gender')}, {persona.get('occupation')} ===")
            page.goto(preview_url, wait_until="domcontentloaded")
            # jfe5 wraps the survey in an iframe that appears after the outer
            # page, so poll for real content rather than warning on the first miss.
            deadline = time.monotonic() + PAGE_SETTLE_TIMEOUT_MS / 1000
            ready = False
            while time.monotonic() < deadline:
                try:
                    if visible_question_blocks(survey_surface(page)) or _has_next_button(survey_surface(page)):
                        ready = True
                        break
                except PlaywrightError:
                    pass
                page.wait_for_timeout(POLL_INTERVAL_MS)
            if not ready:
                if at_login_wall(page):
                    print(f"[Run {run_number}] Qualtrics bounced this preview link to its "
                          f"login page. Run once with --login to save your session, "
                          f"then re-run.")
                    return transcript, history, flags
                print(f"[Run {run_number}] No Qualtrics survey content appeared. "
                      f"Check the preview URL.")

            for _ in range(max_pages):
                surface = survey_surface(page)
                if at_end_of_survey(surface):
                    print(f"[Run {run_number}] Reached end of survey after {survey_page - 1} pages.")
                    break

                signature = page_signature(surface)
                if signature == stuck_signature:
                    stuck_count += 1
                    if stuck_count >= 3:
                        print(f"[Run {run_number}] Page {survey_page} did not advance after 3 "
                              f"attempts (validation error or unsupported question). Stopping.")
                        _save_stop_shot(page, run_number, survey_page, out_dir)
                        break
                else:
                    stuck_signature = signature
                    stuck_count = 0

                if retrying:
                    _drop_page_entries(transcript, history, q_counter, survey_page)
                    retrying = False

                answered = answer_page(surface, client, model, persona,
                                       history, transcript, flags, q_counter, survey_page)
                if not answered:
                    print(f"[Run {run_number}] Page {survey_page}: no question blocks found "
                          f"(info page or unrecognized layout). Trying Next anyway.")

                if not click_next(surface):
                    if at_end_of_survey(survey_surface(page)):
                        print(f"[Run {run_number}] Reached end of survey after {survey_page} pages.")
                    else:
                        print(f"[Run {run_number}] No Next button found on page {survey_page}. Stopping.")
                        _save_stop_shot(page, run_number, survey_page, out_dir)
                    break

                outcome = wait_for_page_change(page, signature)
                if outcome == "end":
                    print(f"[Run {run_number}] Reached end of survey after {survey_page} pages.")
                    break
                if outcome == "changed":
                    survey_page += 1
                elif outcome == "validation":
                    # Same survey page still showing; re-answer it on the next
                    # iteration, guarded by stuck_count.
                    print(f"[Run {run_number}] Page {survey_page}: validation error, retrying page.")
                    retrying = True
                else:  # timeout
                    print(f"[Run {run_number}] Page {survey_page}: page did not change after Next.")
                    retrying = True
            else:
                print(f"[Run {run_number}] Hit max_pages limit ({max_pages}). Stopping as a safety net.")
        finally:
            browser.close()

    return transcript, history, flags


def main():
    parser = argparse.ArgumentParser(description="LLM-driven agent that answers a Qualtrics preview like a human and flags bad branching.")
    parser.add_argument("preview_url", help="The Qualtrics preview link")
    parser.add_argument("--runs", type=int, default=1, help="Number of times to run through the survey")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (no visible window)")
    parser.add_argument("--persona", type=str, default=None, help="Path to a JSON persona file (else GPT invents one per run)")
    parser.add_argument("--model", type=str, default="gpt-4o-mini", help="OpenAI model to use")
    parser.add_argument("--slow", type=int, default=150, help="Milliseconds to slow down each browser action")
    parser.add_argument("--max-pages", type=int, default=200, help="Safety cap on pages visited per run")
    parser.add_argument("--out-dir", type=str, default=None, help="Directory for the JSON logs (default: current directory)")
    parser.add_argument("--auth-file", type=str, default="qualtrics_auth.json",
                        help="Where the saved Qualtrics session is stored")
    parser.add_argument("--login", action="store_true",
                        help="Open a visible browser to log into Qualtrics once and save the session, then exit")
    args = parser.parse_args()

    if args.login:
        sys.exit(0 if save_login_state(args.preview_url, args.auth_file, args.slow) else 1)

    if not Path(args.auth_file).exists():
        print(f"No saved Qualtrics session at {args.auth_file}. Preview links are "
              f"account-protected, so run this once first:\n"
              f"  python {Path(__file__).name} \"<preview_url>\" --login")

    if "/preview/" not in args.preview_url and "PreviewSurvey" not in args.preview_url:
        print("Warning: that URL does not look like a Qualtrics PREVIEW link "
              "(expected '/preview/'). Only run this against previews.")

    client = OpenAI()  # reads OPENAI_API_KEY from environment
    fixed_persona = load_persona(args.persona)
    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(1, args.runs + 1):
        persona = dict(fixed_persona) if fixed_persona else generate_persona(client, args.model)
        transcript, history, flags = run_once(args.preview_url, client, args.model, persona,
                                              args.headless, args.slow, i, args.max_pages,
                                              args.auth_file, out_dir)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = out_dir / f"qualtrics_run_{i}_{timestamp}.json"
        with open(out_file, "w") as f:
            json.dump({"persona": persona, "transcript": transcript,
                       "history": history, "flags": flags},
                      f, indent=2, ensure_ascii=False)

        print(f"[Run {i}] Log saved to {out_file}")
        if flags:
            print(f"[Run {i}] {len(flags)} question(s) flagged as not fitting — see log for details.")


if __name__ == "__main__":
    main()
