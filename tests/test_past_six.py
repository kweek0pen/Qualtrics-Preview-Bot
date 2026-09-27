"""Emma going past her six arguments: the participant asks once when she will
end, and the answer is recorded as a past_six_arguments issue.
    ./venv/bin/python tests/test_past_six.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import emma_chat_headless as ech
import safari_runner as sr

OPINION = "I believe that illegal immigration should be stopped completely."
PARAMS = {
    "model": {"apiKeyEncrypted": "dummy-for-test"},
    "study": {"stopKeyword": "[END CONVERSATION]", "maxUserMessages": 35},
    "initialMessages": [
        {"role": "system", "content": "Participant's confirmed position: your confirmed "
                                      "position is that illegal immigration should stop.\n"
                                      "1. Intervention start: ..."},
        {"role": "assistant", "content": "Hello! I'm Emma Steel. Are you ready?"}],
    "ui": {"stream": True, "assistantMessageOnLoad": False, "hideInitialMessages": False},
}


def fake_backend(route):
    # Emma counting past her limit: 7/6, 8/6, ... until the participant asks
    # when she will end, then she closes.
    body = json.loads(route.request.post_data or "{}")
    msgs = body.get("messages", [])
    users = sum(1 for m in msgs if m.get("role") == "user")
    last_user = next((m.get("content", "") for m in reversed(msgs)
                      if m.get("role") == "user"), "")
    time.sleep(1.5)
    if "When are you going to end" in str(last_user):
        text = "You're right, we're done. Thanks for the debate. [END CONVERSATION]"
    else:
        filler = " ".join(["Evidence sentence with several words in it."] * 20)
        text = f"Counterargument number {users}. {filler}\n\nWhat would change your mind?\n{users}/6"
    route.fulfill(status=200, content_type="text/plain; charset=utf-8", body=text)


_orig_start = ech.HeadlessEmma.start


def start_with_fake_backend(self, timeout=90):
    # Same as the real start(), plus the fake backend route.
    from playwright.sync_api import sync_playwright
    self._pw = sync_playwright().start()
    self._browser = self._pw.chromium.launch(headless=True)
    ctx = self._browser.new_context()
    html = ech._harness_html(self.parent_obj)
    ctx.route(self.origin + ech.HARNESS_PATH,
              lambda r: r.fulfill(status=200, body=html, content_type="text/html"))
    ctx.route(self.origin + "/api/chat", fake_backend)
    ctx.route(self.origin + "/api/db/**", lambda r: r.fulfill(status=200, body="{}"))
    self.page = ctx.new_page()
    self.page.goto(self.origin + ech.HARNESS_PATH)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if self._requests() and self._chat_frame() is not None:
            return
        self.page.wait_for_timeout(500)
    raise ech.ChatError("app did not load")


ech.HeadlessEmma.start = start_with_fake_backend
_real_wait = sr.wait_for_chat_params
sr.read_chat_params = lambda: json.dumps(PARAMS)
handed = {}
sr.hand_back_to_survey = lambda payload, origin, issues: handed.update(
    payload=payload, origin=origin) or True
sr.ask_chat_reply = lambda client, model, persona, log, last, turn, issues: (
    f"Test reply {turn}. I still think the same.", 5.0)

chat_log, issues = [], []
persona = {"opinion_statement": OPINION}
used = sr.run_headless_chat(None, None, persona, chat_log, issues, 3)
print("confirmed position:", persona.get("emma_confirmed_position"))

print("turns used:", used)
for m in chat_log:
    print(f"  {m['turn']:>2} {m['speaker']:<11} {m['text'][:70]!r}")
print("issues:", issues)
data = ech.parse_app_payload(handed.get("payload"))
print("handoff origin:", handed.get("origin"))
print("handoff nextSection:", data and data.get("nextSection"),
      "| messages:", data and len(data.get("messages", [])))


assert used == 8, used                      # 7 turns of arguments, then the question
asked = [m for m in chat_log if m["speaker"] == "participant"
         and m["text"] == sr.ASK_WHEN_END]
assert len(asked) == 1 and asked[0]["turn"] == 8, asked
kinds = [i["kind"] for i in issues]
assert kinds == ["past_six_arguments"], issues
assert "7/6" in issues[0]["detail"] and "[END CONVERSATION]" in issues[0]["detail"], issues[0]
assert data and data["nextSection"] is True
print("PASS")
