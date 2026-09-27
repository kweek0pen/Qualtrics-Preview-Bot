#!/usr/bin/env python3
"""
Drive the Safari tab the user already has open.

Some institutions' Qualtrics logins complete in Safari and nowhere else: every
Chromium build tried (Playwright's bundled one, Brave) dies on the hop between
the survey's datacenter and the account's home datacenter with ERR_TOO_MANY_REDIRECTS, and Playwright cannot attach to a
running Safari to borrow its session. So instead of automating a browser and
logging it in, this drives the browser that is already logged in, through
AppleScript's `do JavaScript`.

REQUIRES, once, in Safari:
    Settings -> Advanced -> "Show features for web developers"
    Settings -> Developer -> "Allow JavaScript from Apple Events"
    (and approve the macOS automation prompt the first time)

Everything here is a thin layer: run some JavaScript in the front tab, get JSON
back. The survey logic lives in safari_runner.py.
"""

import json
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APPLESCRIPT = HERE / "safari_eval.applescript"

# A JS error inside `do JavaScript` comes back as an AppleScript error, so the
# wrapper catches everything and reports it as data instead.
WRAPPER = """
(function () {
  try {
    var __out = (function () { %s })();
    return JSON.stringify({ok: true, value: __out === undefined ? null : __out});
  } catch (e) {
    return JSON.stringify({ok: false, error: String(e && e.message || e)});
  }
})()
"""


class SafariError(RuntimeError):
    pass


# Safari window id the bot drives; None means the front window.
TARGET_WINDOW = None


def _osascript(js_path, timeout=60):
    proc = subprocess.run(
        ["osascript", str(APPLESCRIPT), str(js_path), str(TARGET_WINDOW or "")],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "Allow JavaScript from Apple Events" in err:
            raise SafariError(
                "Safari is refusing injected JavaScript. Enable it once:\n"
                "  Safari -> Settings -> Advanced -> 'Show features for web developers'\n"
                "  Safari -> Settings -> Developer -> 'Allow JavaScript from Apple Events'")
        if "Not authorized to send Apple events" in err:
            raise SafariError(
                "macOS has not granted permission to control Safari. Approve the "
                "automation prompt, or add the terminal app under System Settings -> "
                "Privacy & Security -> Automation.")
        raise SafariError(err or "osascript failed")
    return proc.stdout.strip()


def run_js(body, timeout=60):
    """Run a JS function body in the front Safari tab. `body` must `return` a
    JSON-serialisable value."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(WRAPPER % body)
        path = fh.name
    try:
        raw = _osascript(path, timeout=timeout)
    finally:
        Path(path).unlink(missing_ok=True)

    if not raw:
        raise SafariError("Safari returned nothing (is a tab open?)")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise SafariError(f"Safari returned something that was not JSON: {raw[:200]}")
    if not payload.get("ok"):
        raise SafariError(f"JavaScript error in the page: {payload.get('error')}")
    return payload.get("value")


def front_tab_url():
    proc = subprocess.run(
        ["osascript", "-e",
         f'tell application "Safari" to get URL of {_tab_ref()}'],
        capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise SafariError(proc.stderr.strip() or "could not read the Safari tab")
    return proc.stdout.strip()


def goto(url, settle=3.0):
    subprocess.run(
        ["osascript", "-e",
         f'tell application "Safari" to set URL of {_tab_ref()} to "{url}"'],
        capture_output=True, text=True, timeout=30)
    time.sleep(settle)


def open_window(url, settle=3.0):
    """Open `url` in a new Safari window that belongs to the bot, and point
    every later call at it (by window id, not "front window"), so the user can
    keep using Safari's other windows, or leave the screen off, while it runs."""
    global TARGET_WINDOW
    script = r'''
on run argv
	set u to item 1 of argv
	tell application "Safari"
		make new document with properties {URL:u}
		delay 0.5
		return id of front window
	end tell
end run
'''
    proc = subprocess.run(["osascript", "-e", script, url],
                          capture_output=True, text=True, timeout=30)
    _check(proc, "could not open a Safari window")
    TARGET_WINDOW = proc.stdout.strip()
    time.sleep(settle)
    return TARGET_WINDOW


def _tab_ref():
    return (f"current tab of window id {TARGET_WINDOW}" if TARGET_WINDOW
            else "current tab of front window")


def _check(proc, what):
    """Turn an osascript failure into a SafariError, naming the macOS
    permission that is usually missing."""
    if proc.returncode == 0:
        return
    err = proc.stderr.strip()
    if "not allowed assistive access" in err.lower() or "not authorized" in err.lower():
        raise SafariError(
            "macOS is refusing OS-level clicks/typing. Grant Accessibility and "
            "Automation permission to the terminal app under System Settings -> "
            "Privacy & Security, then run again.")
    raise SafariError(err or what)


def click_paste_enter(x, y, text, timeout=30, replace=False):
    """Click a screen point in Safari, paste text, and press Enter.

    This is intentionally OS-level. Safari's `do JavaScript` cannot reach into a
    cross-origin iframe, but System Events can type where the user could type.

    `replace=True` selects whatever is already in the box first, so a retry
    after a half-finished send does not end up with the text pasted twice.
    """
    script = r'''
on run argv
	set px to item 1 of argv as integer
	set py to item 2 of argv as integer
	set msg to item 3 of argv
	set doReplace to (item 4 of argv is "1")
	set oldClipboard to the clipboard
	tell application "Safari" to activate
	delay 0.2
	tell application "System Events"
		click at {px, py}
		delay 0.2
		if doReplace then
			keystroke "a" using command down
			delay 0.1
		end if
		set the clipboard to msg
		keystroke "v" using command down
		delay 0.4
		key code 36
	end tell
	delay 0.2
	set the clipboard to oldClipboard
	return "ok"
end run
'''
    proc = subprocess.run(
        ["osascript", "-e", script, str(int(x)), str(int(y)), text,
         "1" if replace else "0"],
        capture_output=True, text=True, timeout=timeout)
    _check(proc, "System Events click/paste failed")
    return True


def click_at(x, y, timeout=30):
    """Click one screen point (used to hit a send button after a paste)."""
    script = r'''
on run argv
	set px to item 1 of argv as integer
	set py to item 2 of argv as integer
	tell application "Safari" to activate
	delay 0.2
	tell application "System Events" to click at {px, py}
	return "ok"
end run
'''
    proc = subprocess.run(["osascript", "-e", script, str(int(x)), str(int(y))],
                          capture_output=True, text=True, timeout=timeout)
    _check(proc, "System Events click failed")
    return True


def click_and_enter(x, y, timeout=30):
    """Click a text box and press Enter without retyping anything.

    This is the cheap retry when the text is already sitting in the box but the
    send button click missed.
    """
    script = r'''
on run argv
	set px to item 1 of argv as integer
	set py to item 2 of argv as integer
	tell application "Safari" to activate
	delay 0.2
	tell application "System Events"
		click at {px, py}
		delay 0.2
		key code 36
	end tell
	return "ok"
end run
'''
    proc = subprocess.run(["osascript", "-e", script, str(int(x)), str(int(y))],
                          capture_output=True, text=True, timeout=timeout)
    _check(proc, "System Events click/Enter failed")
    return True


def click_paste_click(input_x, input_y, button_x, button_y, text, timeout=30):
    """Click a text box, paste text, then click a send button."""
    script = r'''
on run argv
	set ix to item 1 of argv as integer
	set iy to item 2 of argv as integer
	set bx to item 3 of argv as integer
	set by to item 4 of argv as integer
	set msg to item 5 of argv
	set oldClipboard to the clipboard
	tell application "Safari" to activate
	delay 0.2
	tell application "System Events"
		click at {ix, iy}
		delay 0.2
		set the clipboard to msg
		keystroke "v" using command down
		delay 0.4
		click at {bx, by}
	end tell
	delay 0.2
	set the clipboard to oldClipboard
	return "ok"
end run
'''
    proc = subprocess.run(
        ["osascript", "-e", script, str(int(input_x)), str(int(input_y)),
         str(int(button_x)), str(int(button_y)), text],
        capture_output=True, text=True, timeout=timeout)
    _check(proc, "System Events click/paste/click failed")
    return True


# Injected ahead of each page-reading snippet passed to `js()`. Each
# osascript call is independent, so there is no state to keep between calls:
# every call re-finds the survey document. Generic to any Qualtrics preview
# page (desktop iframe, mobile mirror, nested chat iframe); survey-specific
# JS lives in the callers, not here.
PREAMBLE = r"""
function D() {
  var best = null, bw = 0, fs = document.querySelectorAll('iframe');
  for (var i = 0; i < fs.length; i++) {
    var d = null;
    try { d = fs[i].contentDocument; } catch (e) {}
    if (!d) continue;
    var w = fs[i].getBoundingClientRect().width;
    if (w > bw) { bw = w; best = d; }
  }
  return best || document;
}
function W() {
  var fs = document.querySelectorAll('iframe'), best = null, bw = 0;
  for (var i = 0; i < fs.length; i++) {
    var w = fs[i].getBoundingClientRect().width;
    if (w > bw) { bw = w; best = fs[i]; }
  }
  return best ? best.contentWindow : window;
}
function clean(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
// Every frame in the tab, at any depth, with its rectangle translated into the
// top document's viewport. Qualtrics nests the preview inside a frame and the
// chat app inside another one below that, and the mobile-preview layout adds a
// third, so a one-level iframe scan misses the chatbot entirely.
function allFrames(maxDepth) {
  var out = [];
  function walk(doc, depth, offX, offY) {
    var fs;
    try { fs = doc.querySelectorAll('iframe, frame'); } catch (e) { return; }
    for (var i = 0; i < fs.length; i++) {
      var f = fs[i], r = f.getBoundingClientRect();
      var left = offX + r.left, top = offY + r.top, inner = null;
      try { inner = f.contentDocument; } catch (e) {}
      out.push({el: f, src: f.src || '', doc: inner, depth: depth,
                rect: {left: left, top: top, width: r.width, height: r.height}});
      if (inner && depth < (maxDepth || 4)) walk(inner, depth + 1, left, top);
    }
  }
  walk(document, 1, 0, 0);
  return out;
}
// Every document the tab will let us read, outermost first.
function allDocs(maxDepth) {
  var out = [document], fs = allFrames(maxDepth);
  for (var i = 0; i < fs.length; i++) if (fs[i].doc) out.push(fs[i].doc);
  return out;
}
function vis(el) {
  if (!el) return false;
  var r = el.getBoundingClientRect();
  if (r.width <= 1 || r.height <= 1) return false;
  var d = el.ownerDocument, w = d.defaultView;
  var st = w.getComputedStyle(el);
  return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
}
function blocks() {
  var d = D();
  var found = Array.prototype.slice.call(d.querySelectorAll('.QuestionOuter')).filter(vis);
  if (!found.length) {
    found = Array.prototype.slice.call(
      d.querySelectorAll('[role="group"], .q-question, fieldset')).filter(vis);
  }
  return found;
}
function qtext(b) {
  var t = b.querySelector('.QuestionText, .q-text, legend');
  var s = t ? clean(t.innerText) : '';
  if (s) return s;
  s = clean(b.innerText);
  return s.split(/(?<=[.?!])\s/)[0] || s.slice(0, 200);
}
function labelFor(el) {
  var d = el.ownerDocument;
  if (el.id) {
    var ls = d.querySelectorAll('label');
    for (var i = 0; i < ls.length; i++) {
      if (ls[i].htmlFor === el.id && clean(ls[i].innerText)) return clean(ls[i].innerText);
    }
  }
  var wrap = el.closest('label');
  if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
  var aria = el.getAttribute('aria-label');
  if (clean(aria)) return clean(aria);
  var li = el.closest('li, td, .Selection, .q-checkbox-container, .q-radio-container');
  if (li && clean(li.innerText)) return clean(li.innerText);
  return el.value || '(unlabeled)';
}
function setValue(el, val) {
  // Qualtrics listens for framework events, so a bare .value assignment is
  // silently ignored on save.
  var proto = el.tagName === 'TEXTAREA'
    ? el.ownerDocument.defaultView.HTMLTextAreaElement.prototype
    : el.ownerDocument.defaultView.HTMLInputElement.prototype;
  var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, val);
  el.dispatchEvent(new (el.ownerDocument.defaultView.Event)('input', {bubbles: true}));
  el.dispatchEvent(new (el.ownerDocument.defaultView.Event)('change', {bubbles: true}));
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
  var d = D();
  return d.querySelector('#NextButton, input#NextButton, button#NextButton') ||
         d.querySelector('button[id*="Next"], input[value="Next" i], button[type="submit"]');
}
"""


def js(body, timeout=90):
    return run_js(PREAMBLE + "\n" + body, timeout=timeout)


def wait_ready(timeout=30):
    """Wait for the document to finish loading."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if run_js("return document.readyState;") == "complete":
                return True
        except SafariError:
            pass
        time.sleep(0.5)
    return False


def check_access():
    """One call that proves the whole bridge works, with a useful message when
    it does not."""
    url = front_tab_url()
    title = run_js("return document.title;")
    return {"url": url, "title": title}


if __name__ == "__main__":
    try:
        info = check_access()
        print(f"Safari bridge OK\n  tab:   {info['url'][:110]}\n  title: {info['title']}")
    except SafariError as e:
        print(f"Safari bridge NOT working:\n{e}")
        raise SystemExit(1)
