#!/usr/bin/env python3
"""
Run the Emma Steel political-chat preview survey. Normally started by ./run.sh.

Some institutions' Qualtrics logins only complete in Safari, so the survey pages are
driven in a Safari window of the bot's own over AppleScript (safari_bridge.py),
which works in the background. The Emma Steel chat is a cross-origin iframe
Safari will not let scripts type into, so the conversation itself runs in a
headless browser with the survey's own chat parameters
(emma_chat_headless.py), and the finished transcript is handed back to the
survey page. No screen clicks, no need to keep Safari in front.

The preview renders the survey twice, in a desktop iframe and a narrower
mobile mirror. Everything below works against the widest one; answering both
would answer every question twice.

USAGE:
    ./run.sh                     # 12 runs; log in when asked
    ./run.sh --runs 1 --show-chat
"""

import argparse
import json
import os
import subprocess
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import safari_bridge as sb
from safari_bridge import SafariError

try:
    from openai import OpenAI
except ImportError:
    print("Missing dependency. Run:\n  pip install openai")
    sys.exit(1)

import conversation_review
import export_chat_to_excel
import write_chat_challenges
from qualtrics_preview_agent import ask_llm
from emma_chat_headless import (
    ARGUMENT_LIMIT,
    END_KEYWORD,
    ChatError,
    HeadlessEmma,
    clean_bot_text,
    parse_app_payload,
    progress_marker,
)
from playwright.sync_api import Error as PlaywrightError
from qualtrics_political_chat_agent import (
    MAX_CHAT_TURNS,
    MAX_REPLY_WORDS,
    CHAT_REPLY_TIMEOUT_MS,
    DEFAULT_PERSONA,
    OPINION_POOL_FILE,
    _lines,
    _strip_own_message,
    _write_run_log,
    ask_chat_reply,
    reading_grade,
    is_opening_opinion_question,
    load_opinion_pool,
    load_persona,
    new_lines_since,
    opinion_block,
    persona_block,
    word_count,
)

# Emma searches the web before most answers, which can take minutes.
CHAT_REPLY_TIMEOUT_S = 240

# Your survey's preview link. Set QUALTRICS_PREVIEW_URL in .env (run.sh loads
# it) or pass --preview-url.
DEFAULT_PREVIEW_URL = os.environ.get("QUALTRICS_PREVIEW_URL", "").strip()


# --------------------------------------------------------------------------
# JavaScript shared by every call
# --------------------------------------------------------------------------

# The generic page-reading preamble (D/W/vis/blocks/qtext/...) and js() moved
# to safari_bridge.py so Trust Game scripts can use them without importing
# this file's chat-specific code.
from safari_bridge import js

# --------------------------------------------------------------------------
# Reading the page
# --------------------------------------------------------------------------

SCAN_JS = r"""
var nb = nextBtn();
var out = {questions: [], hasNext: !!nb, nextVisible: nb ? vis(nb) : false,
           url: D().location ? D().location.href : '',
           validation: false, ended: false, chat: null};
var d = D();

var verr = d.querySelectorAll('.ValidationError, .QuestionOuter .Error, [role=alert]');
for (var i = 0; i < verr.length; i++) { if (vis(verr[i])) out.validation = true; }

if (d.querySelector('#EndOfSurvey, .EndOfSurvey')) out.ended = true;

var bs = blocks();
for (var i = 0; i < bs.length; i++) {
  var b = bs[i], q = {index: i, text: qtext(b), type: null, options: []};
  var radios = Array.prototype.slice.call(b.querySelectorAll('input[type=radio]')).filter(vis);
  var checks = Array.prototype.slice.call(b.querySelectorAll('input[type=checkbox]')).filter(vis);
  var texts = Array.prototype.slice.call(
      b.querySelectorAll('input[type=text], input[type=number], textarea')).filter(vis);
  var sel = Array.prototype.slice.call(b.querySelectorAll('select')).filter(vis)[0];
  var mrows = radios.length ? matrixRows(b) : [];
  var sliders = b.querySelectorAll('input.RealSlider');

  if (sliders.length) {
    // Qualtrics sliders keep their value in a hidden range input; the visible
    // bar is only chrome. Each statement in the question is one slider.
    q.type = 'slider';
    q.sliders = Array.prototype.slice.call(sliders).map(function (s) {
      return {name: s.name, min: Number(s.min || 0), max: Number(s.max || 10)};
    });
    q.min = q.sliders[0].min;
    q.max = q.sliders[0].max;
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
    q.type = 'radio';
    q.options = radios.map(labelFor);
  } else if (checks.length) {
    q.type = 'checkbox';
    q.options = checks.map(labelFor);
  } else if (sel) {
    q.type = 'select';
    q.options = Array.prototype.slice.call(sel.querySelectorAll('option'))
      .filter(function (o) { return (o.value || '').trim(); })
      .map(function (o) { return clean(o.innerText); });
  } else if (texts.length) {
    q.type = 'text';
  } else {
    continue;  // descriptive text block: nothing to answer
  }
  out.questions.push(q);
}
out.signature = out.questions.map(function (q) { return q.text.slice(0, 80); }).join('|')
                + '||' + out.questions.length;
return out;
"""


def scan(timeout=60):
    return js(SCAN_JS, timeout=timeout)


FILL_JS = r"""
var idx = %(idx)d, payload = %(payload)s;
var bs = blocks();
if (idx >= bs.length) return {ok: false, error: 'question gone'};
var b = bs[idx];
var radios = Array.prototype.slice.call(b.querySelectorAll('input[type=radio]')).filter(vis);
var checks = Array.prototype.slice.call(b.querySelectorAll('input[type=checkbox]')).filter(vis);
var texts = Array.prototype.slice.call(
    b.querySelectorAll('input[type=text], input[type=number], textarea')).filter(vis);
var sel = Array.prototype.slice.call(b.querySelectorAll('select')).filter(vis)[0];

if (payload.type === 'matrix') {
  var rows = b.querySelectorAll('tr'), ri = 0, applied = [];
  for (var i = 0; i < rows.length; i++) {
    var rs = Array.prototype.slice.call(rows[i].querySelectorAll('input[type=radio]')).filter(vis);
    if (rs.length < 2) continue;
    var col = payload.cols[ri];
    if (col === null || col === undefined || col < 0 || col >= rs.length) col = 0;
    rs[col].click();
    applied.push(col);
    ri++;
  }
  return {ok: true, applied: applied};
}
if (payload.type === 'slider') {
  var ss = Array.prototype.slice.call(b.querySelectorAll('input.RealSlider'));
  var w = b.ownerDocument.defaultView;
  var setter = Object.getOwnPropertyDescriptor(w.HTMLInputElement.prototype, 'value').set;
  var written = [];
  ss.forEach(function (s, i) {
    var v = payload.values[i];
    if (v === null || v === undefined) v = payload.values[0];
    setter.call(s, String(v));
    s.dispatchEvent(new w.Event('input', {bubbles: true}));
    s.dispatchEvent(new w.Event('change', {bubbles: true}));
    written.push(s.value);
  });
  return {ok: true, written: written};
}
if (payload.type === 'radio') { radios[payload.choice].click(); return {ok: true}; }
if (payload.type === 'checkbox') {
  payload.choices.forEach(function (c) { if (checks[c] && !checks[c].checked) checks[c].click(); });
  return {ok: true};
}
if (payload.type === 'select') {
  var opts = Array.prototype.slice.call(sel.querySelectorAll('option'))
    .filter(function (o) { return (o.value || '').trim(); });
  sel.value = opts[payload.choice].value;
  sel.dispatchEvent(new (sel.ownerDocument.defaultView.Event)('change', {bubbles: true}));
  return {ok: true};
}
if (payload.type === 'text') {
  if (!texts.length) return {ok: false, error: 'no text field'};
  setValue(texts[0], payload.text);
  return {ok: true};
}
return {ok: false, error: 'unknown type'};
"""


def fill(idx, payload):
    return js(FILL_JS % {"idx": idx, "payload": json.dumps(payload)})


def click_next():
    return js("var b = nextBtn(); if (!b || !vis(b)) return false; b.click(); return true;")


def wait_for_survey(timeout=40):
    """The survey lives in an iframe that loads after the top document reports
    ready, so waiting on readyState alone scans an empty page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = scan(timeout=30)
        except SafariError:
            time.sleep(1)
            continue
        if state.get("questions") or state.get("hasNext") or state.get("ended"):
            return state
        time.sleep(1)
    return None


def wait_for_page_change(previous_signature, timeout=40):
    """Qualtrics swaps pages in place, so poll until the page differs from the
    old one and then stops changing — reading it mid-swap sees a blank page."""
    deadline = time.monotonic() + timeout
    last, stable = None, 0
    while time.monotonic() < deadline:
        time.sleep(1.2)
        try:
            state = scan(timeout=30)
        except SafariError:
            continue
        if state.get("ended"):
            return state
        signature = state.get("signature", "")
        if signature == previous_signature:
            last, stable = None, 0
            continue
        if signature == last:
            stable += 1
            # A page with no questions on it may simply be mid-render: Qualtrics
            # paints the frame before the block's content arrives, and accepting
            # that blank moment is how the bot ends up thinking a page is empty.
            needed = 2 if state.get("questions") else 6
            if stable >= needed:
                return state
        else:
            last, stable = signature, 1
    return None


# --------------------------------------------------------------------------
# The chat widget
# --------------------------------------------------------------------------

CHAT_FIND_JS = r"""
var SEND_RE = /^(send|send message|submit|reply|post|→|➤|▶)$/i;
var all = allDocs();
for (var di = 0; di < all.length; di++) {
  var d = all[di];
  var inputs = Array.prototype.slice.call(d.querySelectorAll('textarea, input[type=text]'))
    .filter(vis).filter(function (e) { return !e.disabled && !e.readOnly; });
  if (!inputs.length) continue;

  var buttons = Array.prototype.slice.call(
      d.querySelectorAll('button, input[type=button], input[type=submit], [role=button]'))
    .filter(vis).filter(function (e) { return e.id !== 'NextButton' && e.id !== 'PreviousButton'; });

  // A chat needs somewhere to type AND a way to send that is not the survey's
  // own Next button; a plain text question has only the former. The send
  // control must also sit near the box it belongs to, or a survey text question
  // gets paired with some unrelated "submit" elsewhere on the page and the bot
  // starts a conversation with a form.
  // A survey page has its own questions in it; a chat app's frame does not. On
  // a survey page the pair must share a real container, not just the body,
  // because every element on the page shares the body.
  var isSurveyDoc = d.querySelectorAll('.QuestionOuter').length > 0;
  function near(input, button) {
    var a = input.parentElement;
    for (var k = 0; k < 6 && a; k++) {
      if (isSurveyDoc && (a === d.body || a === d.documentElement)) return false;
      if (a.contains && a.contains(button)) return true;
      a = a.parentElement;
    }
    return false;
  }
  var input = null, send = null;
  for (var ii = 0; ii < inputs.length && !send; ii++) {
    for (var i = 0; i < buttons.length; i++) {
      var lab = clean(buttons[i].innerText || buttons[i].value ||
                      buttons[i].getAttribute('aria-label') || '');
      var sendish = SEND_RE.test(lab) || /send/i.test(buttons[i].className) ||
                    /send/i.test(buttons[i].id);
      if (sendish && near(inputs[ii], buttons[i])) {
        input = inputs[ii];
        send = buttons[i];
        break;
      }
    }
  }
  if (!send) continue;

  input.setAttribute('data-bot-chat-input', '1');
  send.setAttribute('data-bot-chat-send', '1');

  var log = input.parentElement, chosen = null;
  for (var j = 0; j < 8 && log; j++) {
    if (clean(log.innerText).length > 80) { chosen = log; break; }
    log = log.parentElement;
  }
  (chosen || d.body).setAttribute('data-bot-chat-log', '1');
  return {found: true, sameOrigin: true, logChars: clean((chosen || d.body).innerText).length};
}
return null;
"""


def find_chat():
    return js(CHAT_FIND_JS)


def chat_log_text():
    return js("var ds=allDocs();"
              " for (var j=0;j<ds.length;j++){var e=ds[j].querySelector('[data-bot-chat-log]');"
              " if(e) return e.innerText;} return '';") or ""


def chat_input_open():
    return bool(js("var ds=allDocs();"
                   " for (var j=0;j<ds.length;j++){var e=ds[j].querySelector('[data-bot-chat-input]');"
                   " if(e && vis(e) && !e.disabled && !e.readOnly) return true;} return false;"))


READ_APPDATA_JS = r"""
try {
  function parseMaybe(value) {
    if (value == null) return null;
    if (typeof value === 'object') return value;
    if (typeof value !== 'string') return null;
    try { return JSON.parse(value); } catch (e) { return value; }
  }
  var wins = [window], fs = allFrames(4);
  for (var i = 0; i < fs.length; i++) {
    try { if (fs[i].el.contentWindow) wins.push(fs[i].el.contentWindow); } catch (e) {}
  }
  for (var wi = 0; wi < wins.length; wi++) {
    var raw = null;
    try { raw = wins[wi].localStorage.getItem('appData'); } catch (e) {}
    if (!raw) continue;
    var obj = parseMaybe(raw);
    if (Array.isArray(obj)) obj = parseMaybe(obj.join(''));
    if (typeof obj === 'string') obj = parseMaybe(obj);
    if (!obj || typeof obj !== 'object') continue;
    return {nextSection: !!obj.nextSection,
            messages: (obj.messages || []).map(function (m) {
              return {role: m.role, content: m.content};
            })};
  }
  return null;
} catch (e) {
  return {error: String(e && e.message || e)};
}
"""


def read_app_data():
    return js(READ_APPDATA_JS)


# What the page looks like when the chatbot is loading.
# Used to decide whether to keep waiting rather than declare the page empty.
CHAT_PENDING_JS = r"""
var frames = allFrames(4), unreadable = 0, loading = 0;
for (var i = 0; i < frames.length; i++) {
  var f = frames[i];
  if (f.doc) continue;
  unreadable += 1;
  // A frame that exists but has not been given its size yet is still arriving.
  if (f.rect.width < 60 || f.rect.height < 60 || !vis(f.el)) loading += 1;
}
var text = '';
var ds = allDocs();
for (var j = 0; j < ds.length; j++) {
  try { text += ' ' + clean(ds[j].body ? ds[j].body.innerText : ''); } catch (e) {}
}
var hints = /chat|conversation|emma|assistant|message/i.test(text);
var params = false;
try { params = !!localStorage.getItem('chatParams'); } catch (e) {}
return {frames: frames.length, unreadable: unreadable, loading: loading,
        textHints: hints, chatParams: params,
        // What each frame is, for the run log when no chat turns up: the only
        // way to tell "never loaded" from "loaded in markup we do not match".
        detail: frames.map(function (f) {
          return {depth: f.depth, src: (f.src || '').slice(0, 90), readable: !!f.doc,
                  w: Math.round(f.rect.width), h: Math.round(f.rect.height)};
        })};
"""

# Qualtrics keeps the chat state in localStorage, which outlives the page, and
# rebuilds chatParams from the last appData: Emma's `initialMessages` are the
# previous conversation. Left alone, run 2's Emma carries on run 1's debate.
# A fresh participant has none of these keys.
RESET_APPDATA_JS = r"""
var keys = ['appData', 'chatParams', 'hideTextbox'], cleared = [];
for (var i = 0; i < keys.length; i++) {
  try {
    if (localStorage.getItem(keys[i]) !== null) {
      localStorage.removeItem(keys[i]); cleared.push(keys[i]);
    }
  } catch (e) {}
}
return cleared;
"""

READ_CHAT_PARAMS_JS = r"""
try { return localStorage.getItem('chatParams'); } catch (e) { return null; }
"""

# Replays the chat app's own final message into the survey, as if the embedded
# frame had sent it: same data, the app's origin, the frame as its source. The
# survey's listener then stores appData and reveals Next by its own logic.
HANDOFF_JS = r"""
var payload = %(payload)s, origin = %(origin)s;
var called = 0, dispatched = 0, errors = [];
var fs = allFrames(4), seen = [];
for (var i = 0; i < fs.length; i++) {
  var f = fs[i];
  if (f.doc || (f.src && f.src.indexOf(origin) !== 0)) continue;
  var w = f.el.ownerDocument.defaultView, src = null;
  if (seen.indexOf(w) >= 0) continue;
  seen.push(w);
  try { src = f.el.contentWindow; } catch (e) {}
  // The survey's chat JS exposes its own handler; calling it runs exactly
  // the survey's logic (store appData, embedded data, enable Next) without
  // depending on how the browser delivers a synthetic event.
  if (typeof w.messageEventListener === 'function') {
    try { w.messageEventListener({data: payload, origin: origin, source: src}); called++; }
    catch (e) { errors.push(String(e && e.message || e)); }
    continue;
  }
  try {
    var ev;
    try { ev = new w.MessageEvent('message', {data: payload, origin: origin, source: src}); }
    catch (e) { ev = new w.MessageEvent('message', {data: payload, origin: origin}); }
    w.dispatchEvent(ev); dispatched++;
  } catch (e) { errors.push(String(e && e.message || e)); }
}
return {called: called, dispatched: dispatched, errors: errors};
"""

# Last resort when the survey did not react to the replayed message: store the
# transcript where the survey keeps it and un-hide Next.
HANDOFF_FALLBACK_JS = r"""
var payload = %(payload)s;
try { localStorage.setItem('appData', payload); } catch (e) {}
var b = nextBtn();
if (!b) return false;
b.style.display = ''; b.style.visibility = 'visible'; b.style.opacity = '1';
b.disabled = false; b.removeAttribute('disabled');
var p = b.parentElement;
for (var k = 0; k < 4 && p; k++, p = p.parentElement) {
  if (p.style && p.style.display === 'none') p.style.display = '';
}
return vis(b);
"""


def chat_pending():
    """Signs that a chatbot is on this page, even though it may not be loaded."""
    try:
        return js(CHAT_PENDING_JS) or {}
    except SafariError:
        return {}


def read_chat_params():
    try:
        return js(READ_CHAT_PARAMS_JS)
    except SafariError:
        return None


def _norm_text(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


_CONFIRMED_RE = re.compile(r"confirmed position is (?:that )?(.+?)(?:\n|\s\d+\.\s|$)",
                           re.I | re.S)


def wait_for_chat_params(persona, timeout=60):
    """The survey writes the chat's parameters — Emma's system prompt with this
    participant's confirmed position, the model, the encrypted key — into
    localStorage when the chat page loads. The run starts by clearing them, so
    whatever is there now belongs to this run.

    Returns (params JSON string or None, fresh?). Not fresh means the
    parameters already hold a conversation: Emma would resume, not start."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = read_chat_params()
        if raw:
            try:
                params = json.loads(raw)
            except ValueError:
                return raw, False
            initial = params.get("initialMessages") or []
            system = next((m.get("content", "") for m in initial
                           if m.get("role") == "system"), "")
            found = _CONFIRMED_RE.search(system)
            if found:
                persona["emma_confirmed_position"] = found.group(1).strip()
            resumed = any(m.get("role") == "user" for m in initial)
            return raw, not resumed
        time.sleep(1.5)
    return None, False


def looks_like_chat_page(timeout=45):
    """A page with no questions is the chat page if the survey has set chat
    parameters or holds a frame from another origin. Frames load late, so keep
    looking for a while before calling the page empty."""
    deadline = time.monotonic() + timeout
    probe = {}
    while time.monotonic() < deadline:
        probe = chat_pending()
        if probe.get("chatParams") or probe.get("unreadable"):
            return True, probe
        time.sleep(1.5)
    return False, probe


def _bot_entry(turn, page_num, raw):
    text = clean_bot_text(raw)
    entry = {"turn": turn, "speaker": "chatbot", "page": page_num,
             "text": text, "words": word_count(text)}
    if raw and raw.strip() != text:
        entry["raw"] = raw
    return entry


def hand_back_to_survey(payload, origin, issues):
    """Give the survey the finished conversation and wait for its Next button."""
    if not payload:
        issues.append({"turn": 0, "kind": "handoff_failed",
                       "detail": "The chat app never posted a transcript, so there was "
                                 "nothing to give back to the survey."})
        return False
    try:
        result = js(HANDOFF_JS % {"payload": json.dumps(payload), "origin": json.dumps(origin)})
        print(f"  [chat] survey handler called in {result.get('called')} frame(s), "
              f"event sent to {result.get('dispatched')}"
              + (f"; errors: {result['errors']}" if result.get("errors") else ""))
    except SafariError as e:
        issues.append({"turn": 0, "kind": "handoff_failed",
                       "detail": f"Could not pass the transcript to the survey: {e}"})
        return False

    # Qualtrics shows Next on the chat page all along and only refuses to move
    # on until the transcript is stored, so Next being visible proves nothing.
    # Proof is the survey's own appData holding the whole conversation.
    want = parse_app_payload(payload) or {}
    want_n = len(want.get("messages") or [])

    def stored():
        try:
            data = read_app_data() or {}
        except SafariError:
            return False
        return len(data.get("messages") or []) >= want_n and \
            bool(data.get("nextSection")) == bool(want.get("nextSection"))

    print(f"  [chat] handing back {want_n} messages, nextSection="
          f"{bool(want.get('nextSection'))}")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if stored():
            if not want.get("nextSection"):
                issues.append({"turn": 0, "kind": "chat_not_finished",
                               "detail": "The chat app's last transcript did not mark the "
                                         "conversation finished (nextSection false), so the "
                                         "survey keeps Next hidden."})
            return True
        time.sleep(1.0)
    try:
        got = read_app_data() or {}
    except SafariError:
        got = {}

    try:
        js(HANDOFF_FALLBACK_JS % {"payload": json.dumps(payload)})
    except SafariError:
        pass
    ok = stored()
    issues.append({"turn": 0, "kind": "handoff_fallback",
                   "detail": f"The survey did not store the chat transcript when the chat "
                             f"app's final message was delivered (sent {want_n} messages, "
                             f"nextSection={bool(want.get('nextSection'))}; survey held "
                             f"{len(got.get('messages') or [])}, "
                             f"nextSection={bool(got.get('nextSection'))}), so the bot "
                             f"wrote it to appData itself." + ("" if ok else " That failed too.")})
    return ok


def run_headless_chat(client, model, persona, chat_log, issues, page_num,
                      show_browser=False):
    """Hold the Emma Steel conversation in a headless browser.

    Safari cannot type into the cross-origin chat frame without taking over the
    mouse and keyboard, so the conversation runs in emma_chat_headless.py with
    the survey's own chat parameters, and the finished transcript is handed
    back to the survey page in Safari. See that module for why this is the same
    conversation a participant would have.
    """
    params, fresh = wait_for_chat_params(persona)
    if not params:
        issues.append({"turn": 0, "kind": "no_chat_params",
                       "detail": f"Page {page_num} looks like the chat page, but the "
                                 f"survey never wrote chatParams, so the chat could not "
                                 f"be started."})
        return 0
    if not fresh:
        issues.append({"turn": 0, "kind": "chat_params_stale",
                       "detail": "The survey's chatParams already held a conversation "
                                 "(user turns in initialMessages), so Emma resumed an "
                                 "earlier chat instead of starting fresh."})

    print(f"  [chat] page {page_num}: starting the chat app headless")
    emma = HeadlessEmma(params, headless=not show_browser,
                        reply_timeout=CHAT_REPLY_TIMEOUT_S)
    used, finished, payload = 0, False, None
    over_limit, asked_end_turn = None, None
    said_stop_turn, agreed_turn = None, None
    try:
        try:
            emma.start()
        except (ChatError, PlaywrightError) as e:
            issues.append({"turn": 0, "kind": "chat_load_failed",
                           "detail": f"The chat app did not start: {e}"})
            return 0

        # Emma greets, and may post her first argument a few seconds later.
        # Answering the greeting while the argument is still coming would talk
        # past her, so let the opening settle first.
        # The survey's greeting is one of the app's initial messages and is
        # shown without a model call, so the app has posted nothing yet.
        opening = emma.wait_idle(quiet=6.0, timeout=150) or emma.initial_messages()
        for m in opening:
            if m["role"] == "assistant" and m.get("content"):
                chat_log.append(_bot_entry(0, page_num, m["content"]))
        if persona.get("emma_confirmed_position"):
            print(f"  [chat] Emma's confirmed position: "
                  f"{persona['emma_confirmed_position'][:90]!r}")
        if emma.input_state() == "gone" and not chat_log:
            issues.append({"turn": 0, "kind": "chat_load_failed",
                           "detail": "The chat app loaded but showed no text box and no "
                                     "message. Visible text: "
                                     + _norm_text(emma.visible_text())[:200]})
            return 0

        last_bot = chat_log[-1]["text"] if chat_log else None
        if last_bot:
            print(f"  [chat] chatbot opened: {last_bot[:90]!r}")

        for turn in range(1, MAX_CHAT_TURNS + 1):
            if emma.ended():
                finished = True
                break
            if said_stop_turn and not agreed_turn:
                # Emma said she would stop but did not send the stop keyword.
                # Agree plainly instead of debating on, to see if she closes.
                agreed_turn = turn
                text = AGREE_TO_END
                grade = reading_grade(text)
            elif over_limit and not asked_end_turn:
                # Emma is past her six arguments and has not closed. Ask her
                # once, plainly, when she will end, and record the answer.
                asked_end_turn = turn
                text = ASK_WHEN_END
                grade = reading_grade(text)
            else:
                text, grade = ask_chat_reply(client, model, persona, chat_log, last_bot,
                                             turn, issues)
            emma.wait_idle()
            try:
                idx = emma.send(text)
            except (ChatError, PlaywrightError) as e:
                issues.append({"turn": turn, "kind": "send_failed",
                               "detail": f"Turn {turn} could not be sent: {e}"})
                break

            used = turn
            chat_log.append({"turn": turn, "speaker": "participant", "page": page_num,
                             "text": text, "words": word_count(text),
                             "reading_grade": grade})
            print(f"  [chat] turn {turn} sent ({word_count(text)}w, grade {grade}): "
                  f"{text[:75]!r}")

            reply, complete = emma.wait_reply(idx)
            if reply is None:
                issues.append({"turn": turn, "kind": "no_reply",
                               "detail": f"Emma sent nothing within "
                                         f"{CHAT_REPLY_TIMEOUT_S}s of turn {turn}."})
                break
            if not complete:
                issues.append({"turn": turn, "kind": "slow_reply",
                               "detail": f"Emma's reply to turn {turn} was still changing "
                                         f"when the {CHAT_REPLY_TIMEOUT_S}s wait ran out; "
                                         f"the captured text may be cut off."})
            entry = _bot_entry(turn, page_num, reply)
            chat_log.append(entry)
            print(f"  [chat] chatbot replied ({entry['words']}w): {entry['text'][:75]!r}")
            last_bot = entry["text"]

            if turn == asked_end_turn:
                issues.append({"turn": turn, "kind": "past_six_arguments",
                               "detail": f"Emma went past her {ARGUMENT_LIMIT} arguments "
                                         f"without ending ({over_limit}). Asked when she "
                                         f"would end the conversation; she replied: "
                                         f"{entry['text'][:400]}"})
                print(f"  [chat] asked Emma when she will end; she replied: "
                      f"{entry['text'][:90]!r}")
            if turn == agreed_turn:
                closed = END_KEYWORD in reply
                issues.append({"turn": turn, "kind": "stop_without_keyword",
                               "detail": f"Emma said she would stop (turn {said_stop_turn}) "
                                         f"without the stop keyword. The participant agreed "
                                         f"to end; she "
                                         + ("then sent the keyword." if closed else
                                            "still did not send the keyword. She replied: "
                                            + entry["text"][:400])})
                print(f"  [chat] agreed to end; Emma "
                      + ("closed the chat." if closed else
                         f"did not close: {entry['text'][:80]!r}"))
            elif (not said_stop_turn and END_KEYWORD not in reply
                    and (turn == asked_end_turn or SAYS_STOPPING_RE.search(
                        clean_bot_text(reply).replace("\u2019", "'")))):
                # Her answer to "when will you end?" counts too: every one so
                # far has promised to stop without sending the keyword.
                said_stop_turn = turn
            marker = progress_marker(reply)
            if not over_limit and END_KEYWORD not in reply:
                if marker is not None and marker > ARGUMENT_LIMIT:
                    over_limit = f"her reply to turn {turn} was marked {marker}/6"
                elif turn > ARGUMENT_LIMIT:
                    over_limit = (f"she gave argument reply {turn} "
                                  f"(marked {marker}/6)" if marker is not None
                                  else f"she gave argument reply {turn}")

            if END_KEYWORD in reply or emma.ended():
                finished = True
                break

        if finished:
            # The app posts its final transcript (nextSection: true) as it
            # closes; give it a moment rather than racing it.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (emma.app_data() or {}).get("nextSection"):
                emma.page.wait_for_timeout(1000)
            print(f"  [chat] the chatbot closed the conversation after {used} turn(s); "
                  f"app reports nextSection={bool((emma.app_data() or {}).get('nextSection'))}.")
        elif used >= MAX_CHAT_TURNS:
            issues.append({"turn": MAX_CHAT_TURNS, "kind": "turn_cap",
                           "detail": f"The {MAX_CHAT_TURNS}-turn cap was reached before "
                                     "Emma ended the conversation, so the bot stopped "
                                     "rather than letting it run on."})
            print(f"  [chat] hit the {MAX_CHAT_TURNS}-turn cap.")
        payload = emma.raw_payload()
    finally:
        emma.close()

    if hand_back_to_survey(payload, emma.origin, issues):
        print("  [chat] transcript stored in the survey.")
    return used


AGREE_TO_END = "Okay, sounds good, go ahead and wrap it up."

# Emma announcing the end in words ("I'll stop here", "I should end the debate
# here") without sending the keyword that actually closes the chat.
SAYS_STOPPING_RE = re.compile(
    r"\b(I'?ll|I will|I should|I'm going to|let's|let me|we can|we should)\s+"
    r"(now\s+)?(stop|end|wrap|close|conclude)\b|"
    r"\b(end|stop|wrap up|close|conclude)\s+(the|this|our)?\s*"
    r"(conversation|debate|discussion|exchange|chat)\s+(here|now)\b|"
    r"\b(end|stop|wrap up|wrap it up|close|conclude|finish)\s+(it\s+)?(here|now)\b",
    re.I)

ASK_WHEN_END = ("We've gone past the 6 arguments you said you'd make. "
                "When are you going to end this conversation?")


SEND_MSG_JS = r"""
var text = %(text)s;
var ds = [document], fs = document.querySelectorAll('iframe');
for (var i = 0; i < fs.length; i++) {
  try { if (fs[i].contentDocument) ds.push(fs[i].contentDocument); } catch (e) {}
}
var d = null, input = null, send = null;
for (var j = 0; j < ds.length; j++) {
  input = ds[j].querySelector('[data-bot-chat-input]');
  send = ds[j].querySelector('[data-bot-chat-send]');
  if (input) { d = ds[j]; break; }
}
if (!input) return {ok: false, error: 'chat input gone'};
input.focus();
setValue(input, text);
if (send) { send.click(); return {ok: true, via: 'button'}; }
var w = d.defaultView;
input.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
return {ok: true, via: 'enter'};
"""


def send_message(text):
    return js(SEND_MSG_JS % {"text": json.dumps(text)})


def confirm_same_origin_send(text, before_text, timeout=10):
    """Wait until the typed turn shows up in the chat log and the box is clear.

    Setting a React input's value and clicking send can both "succeed" while the
    app ignores the change, so the send is only believed once the message is on
    screen.
    """
    sent = _norm_text(text)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.6)
        try:
            current = chat_log_text()
        except SafariError:
            continue
        if sent and sent in _norm_text(current) and current != before_text:
            return True
    return False


def send_message_confirmed(text, before_text, issues, turn):
    """Send one turn into the same-origin widget, retrying once, and only
    report success when the message is visible in the log."""
    for attempt in (1, 2):
        result = send_message(text)
        if not (result or {}).get("ok"):
            issues.append({"turn": turn, "kind": "send_failed",
                           "detail": f"Turn {turn} could not be sent: "
                                     f"{(result or {}).get('error', 'unknown')}"})
            return False
        if confirm_same_origin_send(text, before_text):
            if attempt > 1:
                issues.append({"turn": turn, "kind": "send_retried",
                               "detail": f"Turn {turn} only posted on the second attempt; "
                                         f"the first send left the message in the box."})
            return True
        print(f"  [chat] turn {turn}: attempt {attempt} never showed in the log, retrying")
    issues.append({"turn": turn, "kind": "send_not_confirmed",
                   "detail": f"Turn {turn} was typed into the chat box but never appeared "
                             f"in the conversation, so the bot stopped rather than "
                             f"recording a message that was never sent."})
    return False


def wait_for_reply(before_text, sent_text, issues, turn):
    """Wait until the chatbot's answer has appeared and stopped growing, so a
    streamed reply is captured whole."""
    deadline = time.monotonic() + CHAT_REPLY_TIMEOUT_MS / 1000
    last, stable = None, 0
    while time.monotonic() < deadline:
        time.sleep(1.2)
        try:
            current = chat_log_text()
        except SafariError:
            continue
        reply = "\n".join(_strip_own_message(
            new_lines_since(before_text, current), sent_text)).strip()
        if not reply:
            last, stable = None, 0
            continue
        if reply == last:
            stable += 1
            if stable >= 2:
                return reply
        else:
            last, stable = reply, 1
    if last:
        issues.append({"turn": turn, "kind": "slow_reply",
                       "detail": f"The chatbot's reply to turn {turn} was still changing "
                                 f"when the {CHAT_REPLY_TIMEOUT_MS // 1000}s wait ran out; "
                                 f"the captured text may be cut off."})
        return last
    issues.append({"turn": turn, "kind": "no_reply",
                   "detail": f"Nothing new appeared in the chat within "
                             f"{CHAT_REPLY_TIMEOUT_MS // 1000}s after sending turn {turn}."})
    return None


def run_chat(client, model, persona, chat_log, issues, page_num):
    print(f"  [chat] widget found on page {page_num}")
    opening = chat_log_text()
    opening_lines = _lines(opening)
    if opening_lines:
        text = "\n".join(opening_lines)
        chat_log.append({"turn": 0, "speaker": "chatbot", "page": page_num,
                         "text": text, "words": word_count(text)})
        print(f"  [chat] chatbot opened: {text[:90]!r}")

    last_bot = "\n".join(opening_lines) if opening_lines else None
    used = 0
    for turn in range(1, MAX_CHAT_TURNS + 1):
        if not chat_input_open():
            print(f"  [chat] the chat box closed after {used} turn(s).")
            break

        text, grade = ask_chat_reply(client, model, persona, chat_log, last_bot, turn, issues)
        before = chat_log_text()
        if not send_message_confirmed(text, before, issues, turn):
            break

        used = turn
        chat_log.append({"turn": turn, "speaker": "participant", "page": page_num,
                         "text": text, "words": word_count(text), "reading_grade": grade})
        print(f"  [chat] turn {turn} sent ({word_count(text)}w, grade {grade}): {text[:75]!r}")

        reply = wait_for_reply(before, text, issues, turn)
        if reply is None:
            break
        chat_log.append({"turn": turn, "speaker": "chatbot", "page": page_num,
                         "text": reply, "words": word_count(reply)})
        print(f"  [chat] chatbot replied ({word_count(reply)}w): {reply[:75]!r}")
        last_bot = reply

    if used >= MAX_CHAT_TURNS and chat_input_open():
        issues.append({"turn": MAX_CHAT_TURNS, "kind": "turn_cap",
                       "detail": f"The {MAX_CHAT_TURNS}-turn cap was reached while the chat "
                                 f"was still open, so the bot ended the conversation rather "
                                 f"than the survey."})
        print(f"  [chat] hit the {MAX_CHAT_TURNS}-turn cap.")
    return used


# --------------------------------------------------------------------------
# One pass through the survey
# --------------------------------------------------------------------------

def answer_questions(state, client, model, persona, history, transcript, flags,
                     q_counter, page_num, issues):
    answered = False
    for q in state["questions"]:
        q_counter[0] += 1
        idx = q_counter[0]
        entry = {"index": idx, "page": page_num, "question": q["text"],
                 "type": q["type"], "answer": None}

        extra = None
        options = q.get("options") or None
        llm_type = q["type"]
        if q["type"] == "matrix":
            extra = {"rows": q.get("rows", []), "cols": q.get("options", [])}
        elif q["type"] == "slider":
            # A slider is a 0-10 scale; ask for it as a choice among the points
            # so the persona picks a number rather than writing prose.
            lo, hi = q.get("min", 0), q.get("max", 10)
            options = [str(v) for v in range(int(lo), int(hi) + 1)]
            llm_type = "radio"

        # The persona's assigned opinion has to reach the free-text questions:
        # the survey's first page is where the participant states it.
        enriched = dict(persona)
        enriched["personality"] = (
            f"{persona_block(persona)}\n\n{opinion_block(persona)}\n\n"
            f"Write free text the way this person really talks: plain words, no "
            f"jargon, no bullet points, at most {MAX_REPLY_WORDS} words.")

        result = ask_llm(client, model, enriched, history, q["text"],
                         llm_type, options, extra) or {}

        try:
            if q["type"] == "matrix":
                rows = q.get("rows", [])
                row_choices = result.get("row_choices") or {}
                cols = []
                for label in rows:
                    c = row_choices.get(label)
                    c = int(c) if isinstance(c, (int, float, str)) and str(c).strip().lstrip("-").isdigit() else None
                    cols.append(c if c is not None else random.randrange(max(len(q.get("options", [1])), 1)))
                fill(q["index"], {"type": "matrix", "cols": cols})
                entry["answer"] = {r: (q["options"][c] if c < len(q["options"]) else c)
                                   for r, c in zip(rows, cols)}
            elif q["type"] == "slider":
                lo, hi = int(q.get("min", 0)), int(q.get("max", 10))
                c = result.get("choice_index")
                value = lo + c if isinstance(c, int) and 0 <= c <= (hi - lo) else (lo + hi) // 2
                n = len(q.get("sliders", [])) or 1
                fill(q["index"], {"type": "slider", "values": [value] * n})
                entry["answer"] = value
            elif q["type"] in ("radio", "select"):
                c = result.get("choice_index")
                c = c if isinstance(c, int) and 0 <= c < len(q["options"]) else random.randrange(len(q["options"]))
                fill(q["index"], {"type": q["type"], "choice": c})
                entry["answer"] = q["options"][c]
            elif q["type"] == "checkbox":
                picks = [c for c in (result.get("choice_indices") or [])
                         if isinstance(c, int) and 0 <= c < len(q["options"])]
                if not picks:
                    picks = [random.randrange(len(q["options"]))]
                fill(q["index"], {"type": "checkbox", "choices": picks})
                entry["answer"] = [q["options"][c] for c in picks]
            else:
                if is_opening_opinion_question(q["text"]) and persona.get("opinion_statement"):
                    text = persona["opinion_statement"]
                else:
                    text = result.get("text") or "I'm not sure how to put it."
                if word_count(text) > MAX_REPLY_WORDS:
                    text = " ".join(text.split()[:MAX_REPLY_WORDS])
                fill(q["index"], {"type": "text", "text": text})
                entry["answer"] = text
        except SafariError as e:
            entry["error"] = str(e)[:200]
            issues.append({"turn": 0, "kind": "fill_failed",
                           "detail": f"Page {page_num}: could not fill "
                                     f"{q['text'][:60]!r} ({str(e)[:100]})"})

        if result.get("fitting") is False:
            flags.append({"index": idx, "question": q["text"],
                          "reason": result.get("reason", "")})
        entry["fitting"] = result.get("fitting")
        entry["fit_reason"] = result.get("reason", "")

        history.append({"question": q["text"], "answer": entry["answer"]})
        transcript.append(entry)
        answered = True
        print(f"  Q{idx} ({q['type']}): {q['text'][:60]!r} -> {str(entry['answer'])[:70]!r}")
    return answered


def run_once(preview_url, client, model, persona, run_number, max_pages=200,
             show_chat=False):
    history, transcript, flags, chat_log, issues = [], [], [], [], []
    q_counter = [0]
    chat_pages = []
    pending_no_chat = {}
    survey_page = 1
    last_signature, stuck = None, 0

    print(f"\n=== Run {run_number}: {persona.get('name')}, {persona.get('education')} ===")
    print(f"    Opinion: {persona.get('opinion_statement', '')[:95]}...")

    state = None
    if run_number == 1:
        # Straight after login the survey is already open on page 1. Reloading
        # it while the new session settles is what sent Safari into a loop.
        first = wait_for_survey(timeout=5)
        if first and first.get("questions") and not first.get("validation"):
            state = first
    if state is None:
        sb.goto(preview_url, settle=4)
        sb.wait_ready()
        if "login" in sb.front_tab_url().lower():
            # A Qualtrics sign-in lasts about an hour, shorter than a
            # full batch. Sign in again through the editor, then carry on.
            print(f"[Run {run_number}] The Qualtrics login expired.")
            sb.goto(editor_url(preview_url) or preview_url, settle=4)
            working_url = wait_for_login(preview_url)
            if working_url:
                preview_url = working_url
            else:
                issues.append({"turn": 0, "kind": "login_wall",
                               "detail": "The Qualtrics login expired and nobody signed "
                                         "in again within 15 minutes."})
                return transcript, chat_log, flags, issues, chat_pages
        state = wait_for_survey()
    if state is None:
        issues.append({"turn": 0, "kind": "no_survey",
                       "detail": "The survey never appeared in the Safari tab after "
                                 "loading the preview link."})
        print(f"[Run {run_number}] The survey did not load. Stopping.")
        return transcript, chat_log, flags, issues, chat_pages
    try:
        js(RESET_APPDATA_JS)
    except SafariError:
        pass

    for _ in range(max_pages):

        if state.get("ended"):
            print(f"[Run {run_number}] Reached the end after {survey_page - 1} pages.")
            break

        signature = state.get("signature", "")
        if signature and signature == last_signature:
            stuck += 1
            if stuck >= 3:
                issues.append({"turn": 0, "kind": "stuck_page",
                               "detail": f"The survey would not advance past page {survey_page}."})
                print(f"[Run {run_number}] Stuck on page {survey_page}. Stopping.")
                break
        else:
            last_signature, stuck = signature, 0

        # Content can still be arriving even after the page settles.
        if not state.get("questions") and not state.get("ended"):
            late = wait_for_survey(timeout=20)
            if late and late.get("questions"):
                state = late

        if survey_page in chat_pages and not state.get("questions"):
            # The chat on this page is over and Next did not move the survey:
            # it is holding the page because the conversation was not finished.
            # Chatting again would start a second conversation.
            issues.append({"turn": 0, "kind": "stuck_after_chat",
                           "detail": f"The survey stayed on the chat page (page "
                                     f"{survey_page}) after the conversation, so the "
                                     f"run stopped there."})
            print(f"[Run {run_number}] Still on the chat page after the chat. Stopping.")
            break

        same_origin_chat = None if state.get("questions") else find_chat()
        is_chat, probe = (False, {})
        if not state.get("questions") and not same_origin_chat:
            print(f"[Run {run_number}] Page {survey_page}: no questions; looking for the chatbot...")
            is_chat, probe = looks_like_chat_page()

        if same_origin_chat:
            chat_pages.append(survey_page)
            pending_no_chat.pop(survey_page, None)
            run_chat(client, model, persona, chat_log, issues, survey_page)
        elif is_chat:
            chat_pages.append(survey_page)
            pending_no_chat.pop(survey_page, None)
            run_headless_chat(client, model, persona, chat_log, issues, survey_page,
                              show_browser=show_chat)
        elif state["questions"]:
            answer_questions(state, client, model, persona, history, transcript,
                             flags, q_counter, survey_page, issues)
        else:
            # Held back rather than reported here: the loop re-visits a page that
            # did not advance, and the chatbot often turns up on that second look.
            pending_no_chat[survey_page] = {
                "turn": 0, "kind": "no_chat_found",
                "detail": f"Page {survey_page} had no ordinary survey questions, no "
                          f"chatParams, and no frame from another origin. Frames: "
                          f"{json.dumps(probe.get('detail', []))[:600]}"}
            print(f"[Run {run_number}] Page {survey_page}: nothing to answer.")

        if not click_next():
            print(f"[Run {run_number}] No Next button on page {survey_page}. Stopping.")
            break

        moved = wait_for_page_change(signature)
        if moved is None:
            print(f"[Run {run_number}] Page {survey_page} did not change after Next.")
            state = scan()
            continue
        state = moved
        survey_page += 1

    for page in sorted(pending_no_chat):
        if page not in chat_pages:
            issues.append(pending_no_chat[page])

    return transcript, chat_log, flags, issues, chat_pages


def editor_url(preview_url):
    """The survey's editor link on the preview's own host.

    Accounts can live on one datacenter (e.g. yul1) while the survey is served
    from another (e.g. <brand>.pdx1). Signing in from the preview link can bounce
    between the two forever ("too many redirects"); signing in from the editor
    link with `LoginAction=EditSection` carries the session over to the survey's
    host, after which
    the preview link opens normally."""
    parts = urlsplit(preview_url)
    survey = re.search(r"SV_[A-Za-z0-9]+", parts.path)
    if not survey:
        return None
    return (f"{parts.scheme}://{parts.netloc}/survey-builder/{survey.group(0)}"
            f"/edit?LoginAction=EditSection")


AGGREGATE_XLSX = "conversation_logs_aggregated.xlsx"
AGGREGATE_LIST = "runs/aggregated_runs.txt"


def update_aggregate(log_file, here):
    """Add one finished run to conversation_logs_aggregated.xlsx in the Emma
    Steel folder. The workbook is rebuilt from the list of every run added so
    far, so it always holds all of them, across batches."""
    listing = here / AGGREGATE_LIST
    listing.parent.mkdir(parents=True, exist_ok=True)
    rel = str(Path(log_file).resolve().relative_to(here))
    known = listing.read_text().split("\n") if listing.exists() else []
    known = [k for k in known if k.strip()]
    if rel not in known:
        known.append(rel)
        listing.write_text("\n".join(known) + "\n")
    paths = [str(here / k) for k in known if (here / k).exists()]
    out = here / AGGREGATE_XLSX
    try:
        export_chat_to_excel.build_workbook(paths, str(out), label_by_batch=True)
    except PermissionError:
        print(f"Could not update {out.name} (is it open in Excel?). The run is still "
              f"listed and will be included next time.")


def preview_candidates(preview_url, host):
    """Preview links worth trying from a signed-in editor on `host`: the
    configured link first, then the same preview on `host`, under both the
    /jfe/ path the editor's Preview button uses and the configured one."""
    parts = urlsplit(preview_url)
    out = [preview_url]
    for path in (re.sub(r"^/jfe\d*/", "/jfe/", parts.path), parts.path):
        url = f"{parts.scheme}://{host}{path}" + (f"?{parts.query}" if parts.query else "")
        if url not in out:
            out.append(url)
    return out


def _survey_loaded(timeout):
    state = wait_for_survey(timeout=timeout)
    return bool(state and (state.get("questions") or state.get("hasNext")))


def wait_for_login(preview_url, timeout=900):
    """Sign in through the survey editor, then open the preview.

    Returns the preview link that loaded (use it for every run), or None.

    The bot opens the editor link in its window. If Qualtrics asks for a login
    the user signs in there, and the bot keeps its hands off until the survey
    editor has loaded: loading a page in the middle of the datacenter hops
    leaves Safari with half a session.

    Qualtrics used to hop the login back to the preview's host (e.g. <brand>.pdx1).
    It can now stop on the account's home datacenter (yul1) instead, often
    after an "Oh no, something went wrong!" page. The editor there still works,
    so from any signed-in editor the bot tries the preview on that host too."""
    deadline = time.monotonic() + timeout
    told = False
    while time.monotonic() < deadline:
        try:
            url = sb.front_tab_url()
        except SafariError:
            url = ""
        parts = urlsplit(url)
        on_qualtrics = parts.netloc.endswith("qualtrics.com")
        if on_qualtrics and "/jfe" in parts.path and "/preview/" in parts.path:
            if _survey_loaded(20):
                return url
        if on_qualtrics and "/survey-builder/" in parts.path:
            if told:
                print(f"Signed in (editor on {parts.netloc}). Opening the preview.")
            # Let the editor finish loading so the session is complete.
            time.sleep(5)
            for candidate in preview_candidates(preview_url, parts.netloc):
                sb.goto(candidate, settle=5)
                if _survey_loaded(30):
                    if candidate != preview_url:
                        print(f"Using the preview on {urlsplit(candidate).netloc}: "
                              f"{candidate[:110]}")
                    return candidate
                print(f"  preview did not load from {candidate[:90]} "
                      f"(now on {sb.front_tab_url()[:90]})")
                now = sb.front_tab_url()
                if urlsplit(now).netloc == parts.netloc and "login" in urlsplit(now).path.lower():
                    break         # session gone on the editor's host; stop trying
            print("\n" + "=" * 70)
            print("Signed in to the editor, but no preview link opened the survey.")
            print("In the editor, click Preview. If the survey opens, copy that tab's")
            print("link and run:  ./run.sh --preview-url '<that link>'")
            print("=" * 70)
            return None
        if not told and "login" in url.lower():
            told = True
            print("\n" + "=" * 70)
            print("Qualtrics wants a login. A Safari window is open on the login page:")
            print("sign in there (including 2FA). The batch starts by itself after that.")
            print("If Qualtrics shows \"Oh no, something went wrong!\", refresh; once the")
            print("survey editor shows, the bot takes over.")
            print("=" * 70)
            subprocess.run(["osascript", "-e",
                            'display notification "Sign in to Qualtrics in the bot\'s '
                            'Safari window." with title "Emma Steel bot" sound name "Glass"'],
                           capture_output=True)
        time.sleep(3)
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Run the political-chat survey through Safari, with the Emma "
                    "Steel conversation held in a headless browser.")
    parser.add_argument("--preview-url", default=DEFAULT_PREVIEW_URL,
                        help="Qualtrics preview link (default: QUALTRICS_PREVIEW_URL from .env)")
    parser.add_argument("--runs", type=int, default=12,
                        help="Passes through the survey (the study calls for 10-15)")
    parser.add_argument("--persona", default="persona_hs_political.json")
    parser.add_argument("--opinions", default=OPINION_POOL_FILE)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--out-dir", default=None,
                        help="Where logs and reports go (default: runs/<timestamp>)")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--show-chat", action="store_true",
                        help="Show the chat browser window instead of running it headless")
    args = parser.parse_args()

    preview_url = args.preview_url
    if not preview_url:
        print("No preview link. Put QUALTRICS_PREVIEW_URL=<your preview link> in .env, "
              "or run:  ./run.sh --preview-url '<your preview link>'")
        sys.exit(1)
    if "/preview/" not in preview_url:
        print("That is not a Qualtrics PREVIEW link. Only run this against previews: "
              "a live link would record real responses.")
        sys.exit(1)

    here = Path(__file__).resolve().parent
    if args.seed is not None:
        random.seed(args.seed)

    client = OpenAI()
    base_persona = load_persona(str(here / args.persona)
                                if not Path(args.persona).exists() else args.persona)
    pool = load_opinion_pool(str(here / args.opinions)
                             if not Path(args.opinions).exists() else args.opinions)
    out_dir = (Path(args.out_dir) if args.out_dir
               else here / "runs" / time.strftime("%Y%m%d_%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Try the preview straight away: with a live Qualtrics session it opens
        # without any login. Only go through the editor when it does not.
        sb.open_window(preview_url, settle=5)
        sb.check_access()
    except SafariError as e:
        print(f"Safari is not reachable:\n{e}")
        sys.exit(1)
    if _survey_loaded(20):
        working_url = preview_url
    else:
        sb.goto(editor_url(preview_url) or preview_url, settle=5)
        working_url = wait_for_login(preview_url)
    if not working_url:
        print("The survey preview never opened. Nothing was run.")
        sys.exit(1)
    preview_url = working_url
    print(f"Driving a dedicated Safari window (id {sb.TARGET_WINDOW}). You can minimise it,")
    print("use other windows, or let the display sleep; do not close it or browse in it.")
    print(f"Output folder: {out_dir}")

    log_files = []
    for i in range(1, args.runs + 1):
        persona = dict(base_persona)
        persona["opinion_statement"] = random.choice(pool)
        try:
            transcript, chat_log, flags, issues, chat_pages = run_once(
                preview_url, client, args.model, persona, i, args.max_pages,
                show_chat=args.show_chat)
        except SafariError as e:
            print(f"[Run {i}] Safari stopped responding: {e}")
            transcript, chat_log, flags, chat_pages = [], [], [], []
            issues = [{"turn": 0, "kind": "safari_error", "detail": str(e)[:300]}]
        log_files.append(_write_run_log(out_dir, i, persona, transcript, chat_log,
                                        flags, issues, chat_pages))
        conversation_review.review_log(client, args.model, log_files[-1])
        update_aggregate(log_files[-1], here)

    if log_files:
        xlsx = out_dir / "qualtrics_chat_transcript.xlsx"
        export_chat_to_excel.build_workbook([str(p) for p in log_files], str(xlsx))
        md = out_dir / "CHAT_CHALLENGES.md"
        write_chat_challenges.write_report([str(p) for p in log_files], str(md))
        print(f"\nWorkbook: {xlsx}\nChallenges report: {md}")


if __name__ == "__main__":
    main()
