#!/usr/bin/env python3
"""
Write CHAT_CHALLENGES.md — what was hard about holding the conversation.

This is deliberately narrow: it reports difficulties the bot had TALKING TO THE
CHATBOT, not every problem in the survey. The audience is the researcher
deciding whether the chat block is ready for real participants, so each entry
says what happened, in which run and turn, and what the bot did about it.

USAGE:
    python write_chat_challenges.py                    # every chat_run_*.json here
    python write_chat_challenges.py --out NOTES.md
"""

import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

MAX_REPLY_WORDS = 150

# Each kind of problem gets a heading and a short explanation of why it matters,
# so the report reads as findings rather than as a log dump.
ISSUE_KINDS = {
    "no_chat_found": (
        "No chat widget was found",
        "The bot walked the whole survey without recognising a chat interface. Either "
        "the conversation never loaded, or it is built in markup the detector does not "
        "match yet — the page dumps from `--dump-pages` are the way to tell which."),
    "no_reply": (
        "The chatbot did not reply",
        "A participant message was sent and nothing came back before the wait ran out. "
        "A real participant would be left staring at the screen."),
    "slow_reply": (
        "The chatbot's reply was still arriving when time ran out",
        "The reply was captured mid-stream, so the transcript for that turn may be "
        "cut off. Long waits also change how participants behave."),
    "turn_cap": (
        "The conversation was ended by the bot, not by the survey",
        "The chat was still open and willing to continue at the turn cap. Worth "
        "checking whether the survey ever tells participants when they may stop."),
    "over_length": (
        "A reply would not come in under the word limit",
        "The model kept writing past the limit even after being asked for a shorter "
        "version, so the message was trimmed before sending."),
    "reading_level": (
        "A reply read above high-school level",
        "The persona has a high school diploma and no college. A reply scoring above "
        "grade 12 does not sound like that person."),
    "llm_failed": (
        "No message could be generated",
        "The model returned nothing usable, so a short neutral line was sent to keep "
        "the conversation moving."),
    "input_failed": (
        "The chat box would not accept typing",
        "Playwright could not type into the input the detector had found."),
    "send_failed": (
        "The message would not send",
        "Neither the send button nor the Enter key submitted the message."),
    "send_not_confirmed": (
        "A message was typed but never posted",
        "The text went into the chat box and every way of submitting it was tried, but "
        "the message never appeared in the conversation. The bot stopped there instead "
        "of carrying on as though the chatbot had received it."),
    "send_retried": (
        "A message only sent on a second attempt",
        "The first submit did nothing and the message stayed in the box until the bot "
        "fell back to another way of sending it. A participant would have had to click "
        "again, and may well have given up."),
    "no_chat_params": (
        "The survey never handed the chat its settings",
        "The chat page loaded, but `chatParams` (Emma's prompt, model and key) never "
        "appeared in the survey's storage, so the chat could not start."),
    "chat_params_stale": (
        "Emma resumed an earlier conversation",
        "The chat settings the survey built already contained user turns, so Emma "
        "picked up an old debate instead of starting fresh. The survey rebuilds these "
        "settings from stored `appData`; a returning participant would see the same."),
    "chat_load_failed": (
        "The chat app did not start",
        "The chat app loaded without a text box or an opening message — usually a "
        "missing or rejected model key, or the app's server being down."),
    "handoff_failed": (
        "The conversation could not be handed back to the survey",
        "The chat finished, but the transcript never reached the survey page, so the "
        "survey had no record of it and no way to move on."),
    "handoff_fallback": (
        "The survey did not react to the finished chat",
        "The chat app's final message was delivered to the survey, but the survey did "
        "not store it, so the bot wrote the transcript to appData itself. Check the "
        "survey's message listener: a real participant could be stuck here."),
    "chat_not_finished": (
        "The chat never told the survey it was finished",
        "The chat app's last transcript did not carry nextSection: true, which is the "
        "only thing that makes the survey show its Next button."),
    "stuck_after_chat": (
        "The survey would not move past the chat",
        "After the conversation the survey stayed on the chat page. It only moves on "
        "once the chat app reports the conversation finished, so this usually follows "
        "a conversation that stopped early (see the other difficulties for that run)."),
    "safari_error": (
        "Safari stopped responding",
        "The run was cut short because Safari would not run the bot's scripts."),
    "stuck_page": (
        "The survey would not advance",
        "The run stopped because a page kept re-showing after Next was clicked."),
    "login_wall": (
        "The run never reached the survey",
        "Qualtrics redirected the preview link to its login page; the saved session "
        "had expired."),
}


def load_runs(paths):
    runs = []
    for path in paths:
        with open(path, "r") as f:
            data = json.load(f)
        data["_file"] = Path(path).name
        runs.append(data)
    return runs


def _conversation_stats(runs):
    turn_counts, bot_words, participant_words, grades = [], [], [], []
    for run in runs:
        chat = run.get("chat", [])
        participant = [m for m in chat if m.get("speaker") == "participant"]
        bot = [m for m in chat if m.get("speaker") == "chatbot"]
        if participant:
            turn_counts.append(len(participant))
        participant_words += [m.get("words", 0) for m in participant]
        bot_words += [m.get("words", 0) for m in bot]
        grades += [m["reading_grade"] for m in participant
                   if m.get("reading_grade") is not None]
    return turn_counts, participant_words, bot_words, grades


def _repeated_bot_lines(runs):
    """A chatbot that says the same thing twice inside one conversation is the
    single most telling problem in a study like this, and no per-turn check can
    see it — so it is computed here, across each run's own transcript."""
    found = {}
    for run in runs:
        counts = Counter(m.get("text", "") for m in run.get("chat", [])
                         if m.get("speaker") == "chatbot" and m.get("text"))
        for text, n in counts.items():
            if n > 1:
                found.setdefault(text, []).append((run.get("run"), n))
    return found


def write_report(paths, out_path):
    runs = load_runs(paths)
    turn_counts, participant_words, bot_words, grades = _conversation_stats(runs)

    by_kind = defaultdict(list)
    for run in runs:
        for issue in run.get("chat_issues", []):
            by_kind[issue.get("kind", "other")].append((run.get("run"), issue))

    total_issues = sum(len(v) for v in by_kind.values())
    repeated = _repeated_bot_lines(runs)

    lines = []
    lines.append("# Talking to the chatbot — what was difficult")
    lines.append("")
    lines.append(f"**Runs covered:** {len(runs)}  ")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"**Logs:** {', '.join(r['_file'] for r in runs)}")
    lines.append("")
    lines.append("The participant persona is an adult with a high school diploma and no "
                 "college. Each run is assigned one political opinion, used word for "
                 "word, and the conversation is capped at ten participant messages of "
                 f"at most {MAX_REPLY_WORDS} words each.")
    lines.append("")

    # --- How the conversations went -------------------------------------
    lines.append("## How the conversations went")
    lines.append("")
    if turn_counts:
        lines.append(f"- Participant messages per run: "
                     f"{min(turn_counts)}–{max(turn_counts)} "
                     f"(average {sum(turn_counts) / len(turn_counts):.1f}).")
    else:
        lines.append("- No participant messages were sent in any run.")
    if participant_words:
        over = sum(1 for w in participant_words if w > MAX_REPLY_WORDS)
        lines.append(f"- Participant message length: {min(participant_words)}–"
                     f"{max(participant_words)} words "
                     f"(average {sum(participant_words) / len(participant_words):.0f}). "
                     f"{over} message(s) exceeded the {MAX_REPLY_WORDS}-word limit.")
    if bot_words:
        lines.append(f"- Chatbot message length: {min(bot_words)}–{max(bot_words)} words "
                     f"(average {sum(bot_words) / len(bot_words):.0f}).")
    if grades:
        lines.append(f"- Participant reading level (Flesch-Kincaid): "
                     f"{min(grades):.1f}–{max(grades):.1f} "
                     f"(average {sum(grades) / len(grades):.1f}). "
                     f"Grade 6–12 is the band this persona should write in.")
    lines.append("")

    # --- The difficulties ------------------------------------------------
    lines.append("## Difficulties")
    lines.append("")
    if not total_issues:
        lines.append("No problems were recorded. Every run found the chat, sent its "
                     "messages, and got a reply to each one within the wait window.")
        lines.append("")
    else:
        ordered = sorted(by_kind.items(), key=lambda kv: -len(kv[1]))
        for kind, entries in ordered:
            title, why = ISSUE_KINDS.get(
                kind, (kind.replace("_", " ").capitalize(), ""))
            affected = sorted({run_no for run_no, _ in entries if run_no is not None})
            lines.append(f"### {title}")
            lines.append("")
            lines.append(f"**Runs affected:** {', '.join(str(r) for r in affected) or '—'} "
                         f"({len(entries)} occurrence(s))")
            lines.append("")
            if why:
                lines.append(f"{why}")
                lines.append("")
            for run_no, issue in entries[:12]:
                turn = issue.get("turn")
                where = f"run {run_no}, turn {turn}" if turn else f"run {run_no}"
                lines.append(f"- *{where}* — {issue.get('detail', '')}")
            if len(entries) > 12:
                lines.append(f"- …and {len(entries) - 12} more of the same kind.")
            lines.append("")

    # --- Repetition ------------------------------------------------------
    lines.append("## Repetition from the chatbot")
    lines.append("")
    if repeated:
        lines.append("These lines were sent by the chatbot more than once inside a single "
                     "conversation. A participant notices this immediately, and it "
                     "undercuts the sense that anything is listening.")
        lines.append("")
        for text, where in sorted(repeated.items(), key=lambda kv: -max(n for _, n in kv[1]))[:10]:
            spots = "; ".join(f"run {r}: ×{n}" for r, n in where)
            snippet = text if len(text) <= 160 else text[:157] + "..."
            lines.append(f"- \"{snippet}\" — {spots}")
        lines.append("")
    else:
        lines.append("No chatbot line was repeated within a single conversation.")
        lines.append("")

    # --- Per-run detail --------------------------------------------------
    lines.append("## Run by run")
    lines.append("")
    lines.append("| Run | Opinion assigned | Participant turns | Chatbot turns | Issues |")
    lines.append("|---|---|---|---|---|")
    for run in runs:
        chat = run.get("chat", [])
        p = sum(1 for m in chat if m.get("speaker") == "participant")
        b = sum(1 for m in chat if m.get("speaker") == "chatbot")
        opinion = run.get("opinion_statement", "")
        short = (opinion[:70] + "…") if len(opinion) > 70 else opinion
        lines.append(f"| {run.get('run', '')} | {short} | {p} | {b} | "
                     f"{len(run.get('chat_issues', []))} |")
    lines.append("")

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path}: {len(runs)} run(s), {total_issues} issue(s) reported.")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Write the chat challenges report.")
    parser.add_argument("logs", nargs="*", help="Run logs (default: chat_run_*.json here)")
    parser.add_argument("--out", default="CHAT_CHALLENGES.md")
    args = parser.parse_args()

    paths = args.logs or sorted(glob.glob("chat_run_*.json"))
    if not paths:
        print("No run logs found. Run the agent first, or pass log paths.")
        sys.exit(1)
    write_report(paths, args.out)


if __name__ == "__main__":
    main()
