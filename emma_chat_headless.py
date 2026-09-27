#!/usr/bin/env python3
"""
Hold the Emma Steel conversation in a headless browser, with no screen needed.

WHY THIS EXISTS

The survey embeds Emma's chat app (a SvelteKit app on Vercel) in a
cross-origin iframe. Safari's `do JavaScript` cannot reach into that frame, so
the old runner clicked and pasted at guessed screen coordinates. That needed
Safari in front, the display on, and still missed whenever the frame grew
past the bottom of the window.

The app itself does not care who its parent is. From its bundle:

  * In an iframe, it posts `{"requestData": true}` to its parent every second
    until the parent answers with the chat parameters (the same JSON the survey
    keeps in `localStorage.chatParams`). It only accepts that answer from an
    allow-listed origin; its own origin is on the list.
  * After every message it posts the whole conversation back to the parent as
    a JSON array of string chunks of
    `{messages, nextSection, chatParams, userAgentInfo, ...}` — exactly what the
    survey stores as `appData`, and what reveals the survey's Next button.

So this module serves a ten-line parent page *on the app's own origin*
(intercepted by Playwright, never touching the real server), hands the app the
survey's real parameters, and types into the app's real <textarea>. Same app,
same backend, same system prompt, same end-of-chat logic — only the parent
page differs. The final payload is then replayed into the Safari survey by
safari_runner.py, so Qualtrics records it as if the embedded frame sent it.
"""

import json
import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError

DEFAULT_APP_URL = "https://vegapunk-sunnyquay.vercel.app"
HARNESS_PATH = "/__preview_bot_parent"
END_KEYWORD = "[END CONVERSATION]"

# Emma's app wraps its web-search step in an HTML comment ahead of the real
# answer ("<!--<div ...>Online search results ... -->The core claim ..."), and
# that whole string is what lands in appData. The participant never sees the
# comment, so neither the persona nor the report should.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TAG_RE = re.compile(r"</?(div|span|hr|br|p|b|i|em|strong)\b[^>]*>", re.I)
# Emma sometimes counts past the limit ("7/6"), so any count ends a message.
_PROGRESS_RE = re.compile(r"\b(\d{1,2})\s*/\s*6\s*$")
ARGUMENT_LIMIT = 6


def progress_marker(raw):
    """Emma's `n/6` count at the end of a message, or None."""
    m = _PROGRESS_RE.search(clean_bot_text(raw or ""))
    return int(m.group(1)) if m else None


def clean_bot_text(raw):
    """What the participant actually reads on screen for one chatbot message."""
    text = _COMMENT_RE.sub("", raw or "")
    if "<!--" in text:                      # comment still streaming in
        text = text.split("<!--", 1)[0]
    text = _TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def bot_reply_looks_finished(raw):
    """Emma closes every argument with its `n/6` marker and her last message
    with the stop keyword. Either one means the message is whole."""
    if not raw:
        return False
    if "<!--" in raw and "-->" not in raw.rsplit("<!--", 1)[1]:
        return False
    return END_KEYWORD in raw or bool(_PROGRESS_RE.search(clean_bot_text(raw)))


def parse_app_payload(raw):
    """The app posts JSON.stringify([chunk, chunk, ...]) of one JSON object."""
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, list):
            obj = json.loads("".join(obj))
        if isinstance(obj, str):
            obj = json.loads(obj)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _norm(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _harness_html(parent_obj):
    # `</` is escaped so a system prompt containing "</script>" cannot end the tag.
    literal = json.dumps(parent_obj).replace("</", "<\\/")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>preview bot parent</title></head>
<body style="margin:0">
<iframe id="chat" src="/" style="width:100%;height:95vh;border:0"></iframe>
<script>
window.__parentObj = {literal};
window.__appData = null;
window.__posts = 0;
window.__requests = 0;
window.addEventListener('message', function (e) {{
  var obj = null;
  try {{ obj = JSON.parse(e.data); }} catch (err) {{}}
  if (obj && obj.requestData) {{
    window.__requests++;
    document.getElementById('chat').contentWindow.postMessage(window.__parentObj, '*');
    return;
  }}
  window.__appData = e.data;
  window.__posts++;
}});
</script>
</body></html>"""


class ChatError(RuntimeError):
    pass


class HeadlessEmma:
    """One conversation with the real chat app, in a headless browser."""

    def __init__(self, parent_obj, headless=True, reply_timeout=240):
        if not isinstance(parent_obj, str):
            parent_obj = json.dumps(parent_obj)
        try:
            params = json.loads(parent_obj)
        except ValueError:
            raise ChatError("chatParams from the survey is not valid JSON")
        app_url = (params.get("appURL_") or DEFAULT_APP_URL).strip()
        parts = urlsplit(app_url if "://" in app_url else "https://" + app_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.parent_obj = parent_obj
        self.headless = headless
        self.reply_timeout = reply_timeout
        self._pw = self._browser = self.page = None

    # ---- lifecycle -------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self, timeout=90):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        context = self._browser.new_context(viewport={"width": 1000, "height": 900})
        html = _harness_html(self.parent_obj)
        context.route(self.origin + HARNESS_PATH,
                      lambda route: route.fulfill(status=200, body=html,
                                                  content_type="text/html; charset=utf-8"))
        self.page = context.new_page()
        self.page.goto(self.origin + HARNESS_PATH, wait_until="domcontentloaded")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._requests() and self._chat_frame() is not None:
                break
            self.page.wait_for_timeout(500)
        else:
            raise ChatError("The chat app never asked for its parameters; it did not load.")

    def close(self):
        for thing in (self._browser, self._pw):
            try:
                if thing is self._pw and thing:
                    thing.stop()
                elif thing:
                    thing.close()
            except Exception:
                pass
        self._pw = self._browser = self.page = None

    # ---- reading ---------------------------------------------------------

    def _requests(self):
        try:
            return self.page.evaluate("window.__requests")
        except PlaywrightError:
            return 0

    def _chat_frame(self):
        for frame in self.page.frames:
            if frame is not self.page.main_frame and frame.url.startswith(self.origin):
                return frame
        return None

    def raw_payload(self):
        """The last message the app posted to its parent, exactly as sent."""
        try:
            return self.page.evaluate("window.__appData")
        except PlaywrightError:
            return None

    def app_data(self):
        return parse_app_payload(self.raw_payload())

    def messages(self):
        data = self.app_data() or {}
        return [m for m in (data.get("messages") or []) if m.get("role") in ("user", "assistant")]

    def initial_messages(self):
        """The visible opening lines the survey pre-loads into the app."""
        try:
            initial = json.loads(self.parent_obj).get("initialMessages") or []
        except ValueError:
            return []
        return [m for m in initial if m.get("role") in ("user", "assistant")]

    def scroll_to_bottom(self):
        """Scroll the conversation to its end, as a participant reading it would.

        The app disables its text box whenever #scrollElement is not scrolled
        to the bottom (its scroll handler sets the `disabled` store), so the
        participant has to read to the end before replying. Once Emma's replies
        are taller than the frame, usually from her second argument on, the
        box stays locked until something scrolls down. Nobody did, so the chat
        stalled after two turns."""
        frame = self._chat_frame()
        if frame is None:
            return
        try:
            frame.evaluate("""() => {
                const el = document.getElementById('scrollElement');
                if (el) {
                    el.scrollTop = el.scrollHeight;
                    el.dispatchEvent(new Event('scroll'));
                }
                window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
            }""")
        except PlaywrightError:
            pass

    def input_state(self):
        """'open', 'busy' (present but disabled while Emma answers) or 'gone'."""
        frame = self._chat_frame()
        if frame is None:
            return "gone"
        try:
            box = frame.locator("textarea").first
            if box.count() == 0 or not box.is_visible():
                return "gone"
            if not box.is_enabled():
                # Locked either while Emma answers or because the chat is not
                # scrolled to the end. Scrolling clears only the second case.
                self.scroll_to_bottom()
                frame.wait_for_timeout(300)
            return "open" if box.is_enabled() else "busy"
        except PlaywrightError:
            return "gone"

    def visible_text(self):
        frame = self._chat_frame()
        try:
            return frame.evaluate("document.body ? document.body.innerText : ''") if frame else ""
        except PlaywrightError:
            return ""

    # ---- waiting ---------------------------------------------------------

    def wait_idle(self, quiet=4.0, timeout=300):
        """Until Emma has spoken last, the box is usable, and nothing has changed
        for `quiet` seconds (her search step pauses mid-message)."""
        deadline = time.monotonic() + timeout
        last_sig, since = None, time.monotonic()
        while time.monotonic() < deadline:
            msgs = self.messages()
            state = self.input_state()
            sig = (len(msgs), msgs[-1]["content"] if msgs else "", state)
            if sig != last_sig:
                last_sig, since = sig, time.monotonic()
            held = time.monotonic() - since
            bot_last = bool(msgs) and msgs[-1]["role"] == "assistant"
            finished = bot_last and bot_reply_looks_finished(msgs[-1]["content"])
            if state == "gone" and held >= 2:
                return msgs
            if state == "open" and bot_last and (held >= quiet or (finished and held >= 1.5)):
                return msgs
            if state == "open" and not msgs and held >= quiet * 2:
                return msgs                   # app waits for the participant to open
            self.page.wait_for_timeout(700)
        return self.messages()

    # ---- talking ---------------------------------------------------------

    def _box_value(self):
        frame = self._chat_frame()
        try:
            return frame.locator("textarea").first.input_value() if frame else None
        except PlaywrightError:
            return None

    def send(self, text, timeout=300):
        """Type one participant turn and press Enter, once.

        Returns the transcript length before the send; pass it to wait_reply.

        The app posts its transcript to the parent *before* appending the new
        user message, so the message only shows up in the transcript together
        with Emma's answer, often a minute later. What proves the send
        immediately is what a participant sees: the box empties and locks.
        Re-sending on a timer is what produced duplicate turns, so the only
        retry is for text still sitting, unsent, in an open box."""
        before = len(self.messages())
        frame = self._chat_frame()
        if frame is None:
            raise ChatError("The chat frame is gone.")
        box = frame.locator("textarea").first
        deadline = time.monotonic() + timeout
        while self.input_state() == "busy" and time.monotonic() < deadline:
            self.page.wait_for_timeout(1000)
        if self.input_state() != "open":
            raise ChatError(f"The chat box is still {self.input_state()} after "
                            f"{timeout}s, so the turn cannot be typed.")

        for attempt in (1, 2):
            box.click()
            box.fill(text)
            box.press("Enter")
            end = time.monotonic() + 10
            while time.monotonic() < end:
                self.page.wait_for_timeout(500)
                value = self._box_value()
                if self.input_state() != "open" or not (value or "").strip():
                    return before
                if len(self.messages()) > before:
                    return before
            # Still open with our text in it: Enter did not take.
        raise ChatError("The turn was typed and Enter pressed twice, but the text "
                        "stayed in the box.")

    def wait_reply(self, before):
        """Emma's whole answer to the first user message after index `before`.

        Returns (raw text, finished?) or (None, False) on timeout."""
        deadline = time.monotonic() + self.reply_timeout
        last, stable = None, 0
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(1200)
            msgs = self.messages()
            mine = next((i for i in range(before, len(msgs))
                         if msgs[i]["role"] == "user"), None)
            if mine is None:
                continue
            tail = [m["content"] for m in msgs[mine + 1:]
                    if m["role"] == "assistant" and m.get("content")]
            if not tail:
                continue
            reply = "\n\n".join(t.strip() for t in tail)
            if END_KEYWORD in reply:
                return reply, True           # Emma's closing line can be the keyword alone
            if not clean_bot_text(reply):
                last, stable = reply, 0      # only the search comment so far
                continue
            state = self.input_state()
            if reply == last:
                stable += 1
                if bot_reply_looks_finished(tail[-1]) and stable >= 1:
                    return reply, True
                if state in ("open", "gone") and stable >= 3:
                    return reply, True
                if stable >= 8:
                    return reply, True
            else:
                last, stable = reply, 0
        return last, False

    def ended(self):
        data = self.app_data() or {}
        return bool(data.get("nextSection")) or self.input_state() == "gone"


if __name__ == "__main__":
    # Smoke test of the harness alone: loads the app and shows that it asks for
    # parameters. Without the survey's real chatParams (which carry the
    # encrypted model key) it cannot start a real conversation.
    import sys
    params = sys.argv[1] if len(sys.argv) > 1 else json.dumps({"model": {}})
    with HeadlessEmma(params) as emma:
        print("app loaded; parameter requests:", emma._requests())
        emma.page.wait_for_timeout(3000)
        print("input:", emma.input_state())
        print("text:", emma.visible_text()[:300])
