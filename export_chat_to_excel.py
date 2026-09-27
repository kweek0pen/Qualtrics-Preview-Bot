#!/usr/bin/env python3
"""
Turn chat_run_*.json logs into one .xlsx workbook.

Based on export_run_to_excel.py, with the difference that this survey produces
two kinds of record: ordinary survey answers, and a two-party conversation.
They are kept on separate sheets because they read differently — a survey answer
is a question/answer pair, a chat turn is a speaker and what they said.

USAGE:
    python export_chat_to_excel.py                       # every chat_run_*.json here
    python export_chat_to_excel.py run1.json run2.json
    python export_chat_to_excel.py --out transcript.xlsx

SHEETS:
    Summary       one row per run: persona, assigned opinion, counts, issues
    Conversation  one row per chat turn: who spoke, what they said, word count
                  (aggregated workbook: one row per run, columns chat0,
                  participant1, chat1, ..., chat{n})
    Survey        one row per ordinary survey question and the answer given
    Issues        one row per problem the bot hit while talking to the chatbot
"""

import argparse
import glob
import json
import sys
from collections import Counter
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    print("Missing dependency. Run:\n  pip install openpyxl")
    sys.exit(1)


HEADER_FILL = PatternFill("solid", fgColor="DDDDDD")
PARTICIPANT_FILL = PatternFill("solid", fgColor="E8F1FB")
BOT_FILL = PatternFill("solid", fgColor="F3F0FA")
PROBLEM_FILL = PatternFill("solid", fgColor="FFE0E0")

MAX_REPLY_WORDS = 150


def load_runs(paths):
    runs = []
    for path in paths:
        with open(path, "r") as f:
            data = json.load(f)
        data["_file"] = Path(path).name
        data["_batch"] = Path(path).resolve().parent.name
        runs.append(data)
    return runs


def _run_label(run):
    """Run numbers restart in every batch, so the aggregated workbook labels
    each run by its batch folder as well."""
    return run.get("_label") or run.get("run", "")


def _style_header(ws, widths):
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def _answer_text(answer):
    if answer is None:
        return ""
    if isinstance(answer, dict):
        return "\n".join(f"{k}: {v}" for k, v in answer.items())
    if isinstance(answer, list):
        return "\n".join(str(a) for a in answer)
    return str(answer)


def build_summary_sheet(wb, runs):
    ws = wb.create_sheet("Summary", 0)
    ws.append(["Run", "Log file", "Persona", "Assigned opinion (verbatim)",
               "Position Emma was told to argue against", "Survey answers", "Participant turns", "Chatbot turns",
               "Longest participant turn (words)", "Avg reading grade",
               "Chat page(s)", "Issues"])
    for run in runs:
        chat = run.get("chat", [])
        participant = [m for m in chat if m.get("speaker") == "participant"]
        bot = [m for m in chat if m.get("speaker") == "chatbot"]
        grades = [m["reading_grade"] for m in participant
                  if m.get("reading_grade") is not None]
        persona = run.get("persona", {})
        ws.append([
            _run_label(run),
            run.get("_file", ""),
            f"{persona.get('name', '')}, {persona.get('age', '')}, "
            f"{persona.get('education', '')}",
            run.get("opinion_statement", ""),
            persona.get("emma_confirmed_position", ""),
            len(run.get("transcript", [])),
            len(participant),
            len(bot),
            max((m.get("words", 0) for m in participant), default=0),
            round(sum(grades) / len(grades), 1) if grades else "",
            ", ".join(str(p) for p in run.get("chat_pages", [])),
            len(run.get("chat_issues", [])),
        ])
        row = ws.max_row
        for col in (3, 4, 5):
            ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
        if run.get("chat_issues"):
            ws.cell(row=row, column=12).fill = PROBLEM_FILL
    _style_header(ws, [6, 30, 30, 60, 50, 14, 16, 14, 26, 16, 14, 8])
    return ws


def build_conversation_sheet(wb, runs):
    """The sheet the researcher actually reads: the conversation itself, in
    order, with the participant and the chatbot visually separated."""
    ws = wb.create_sheet("Conversation")
    ws.append(["Run", "Opinion topic", "Page", "Turn", "Speaker", "Message",
               "Words", "Reading grade", "Over 150 words?"])
    for run in runs:
        opinion = run.get("opinion_statement", "")
        topic = (opinion[:60] + "...") if len(opinion) > 60 else opinion
        for m in run.get("chat", []):
            words = m.get("words", 0)
            is_participant = m.get("speaker") == "participant"
            ws.append([
                _run_label(run),
                topic,
                m.get("page", ""),
                m.get("turn", ""),
                "Participant" if is_participant else "Chatbot",
                m.get("text", ""),
                words,
                m.get("reading_grade", ""),
                "YES" if (is_participant and words > MAX_REPLY_WORDS) else "",
            ])
            row = ws.max_row
            fill = PARTICIPANT_FILL if is_participant else BOT_FILL
            for col in range(1, 10):
                ws.cell(row=row, column=col).fill = fill
            for col in (2, 6):
                ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
            if is_participant and words > MAX_REPLY_WORDS:
                ws.cell(row=row, column=9).fill = PROBLEM_FILL
    _style_header(ws, [6, 34, 7, 7, 13, 90, 8, 14, 16])
    return ws


def build_wide_conversation_sheet(wb, runs):
    """One row per run, the conversation read left to right:
    chat0, participant1, chat1, participant2, ..., chat{n}.

    'chatN' is the chatbot's Nth message (chat0 is its greeting) and
    'participantN' is the participant's Nth message, the reply to chat{N-1}.
    Columns run as far as the longest conversation in the workbook; shorter
    runs leave the rest blank."""
    ws = wb.create_sheet("Conversation")
    turns = [(m.get("speaker"), m.get("turn") or 0)
             for run in runs for m in run.get("chat", [])]
    last_chat = max([t for s, t in turns if s == "chatbot"], default=0)
    last_part = max([t for s, t in turns if s == "participant"], default=0)
    columns = []                              # (header, speaker, turn)
    for n in range(max(last_chat, last_part) + 1):
        if n >= 1 and n <= last_part:
            columns.append((f"participant{n}", "participant", n))
        if n <= last_chat:
            columns.append((f"chat{n}", "chatbot", n))
    header = ["opinion topic"] + [c[0] for c in columns] + ["overall opinions"]
    ws.append(header)
    for run in runs:
        said = {(m.get("speaker"), m.get("turn")): m.get("text", "")
                for m in run.get("chat", [])}
        ws.append([run.get("opinion_statement", "")]
                  + [said.get((speaker, turn), "") for _, speaker, turn in columns]
                  + [run.get("conversation_review", "")])
        r = ws.max_row
        for col in range(1, len(header) + 1):
            ws.cell(row=r, column=col).alignment = Alignment(wrap_text=True, vertical="top")
            if header[col - 1].startswith("participant"):
                ws.cell(row=r, column=col).fill = PARTICIPANT_FILL
            elif header[col - 1].startswith("chat"):
                ws.cell(row=r, column=col).fill = BOT_FILL
    _style_header(ws, [40] + [50] * (len(header) - 2) + [60])
    return ws


def build_survey_sheet(wb, runs):
    ws = wb.create_sheet("Survey")
    ws.append(["Run", "Q#", "Page", "Type", "What the survey showed",
               "What the participant answered", "Fits the flow?", "Reason", "Fill error"])
    for run in runs:
        for e in run.get("transcript", []):
            ws.append([
                _run_label(run),
                e.get("index"),
                e.get("page"),
                e.get("type"),
                e.get("question", ""),
                _answer_text(e.get("answer")),
                "" if e.get("fitting") is None else ("yes" if e["fitting"] else "NO"),
                e.get("fit_reason", ""),
                e.get("error", ""),
            ])
            row = ws.max_row
            for col in (5, 6, 8):
                ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
            if e.get("fitting") is False:
                for col in range(1, 10):
                    ws.cell(row=row, column=col).fill = PROBLEM_FILL
    _style_header(ws, [6, 6, 7, 10, 58, 58, 13, 30, 24])
    return ws


def build_issues_sheet(wb, runs):
    ws = wb.create_sheet("Issues")
    ws.append(["Run", "Turn", "Kind", "What happened"])
    for run in runs:
        for issue in run.get("chat_issues", []):
            ws.append([_run_label(run), issue.get("turn", ""),
                       issue.get("kind", ""), issue.get("detail", "")])
            ws.cell(row=ws.max_row, column=4).alignment = Alignment(wrap_text=True,
                                                                   vertical="top")
    _style_header(ws, [6, 7, 20, 100])
    return ws


def build_workbook(paths, out_path, label_by_batch=False):
    runs = load_runs(paths)
    if label_by_batch:
        for run in runs:
            run["_label"] = f"{run['_batch']} #{run.get('run', '')}"
    wb = Workbook()
    wb.remove(wb.active)
    build_summary_sheet(wb, runs)
    if label_by_batch:
        build_wide_conversation_sheet(wb, runs)
    else:
        build_conversation_sheet(wb, runs)
    build_survey_sheet(wb, runs)
    build_issues_sheet(wb, runs)
    if label_by_batch:
        for ws in wb.worksheets:
            if ws.title != "Conversation":
                ws.column_dimensions["A"].width = 22
    wb.save(out_path)

    turns = sum(len(r.get("chat", [])) for r in runs)
    answers = sum(len(r.get("transcript", [])) for r in runs)
    issues = sum(len(r.get("chat_issues", [])) for r in runs)
    print(f"Wrote {out_path}: {len(runs)} run(s), {answers} survey answer(s), "
          f"{turns} chat message(s), {issues} issue(s).")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Export chat run logs to Excel.")
    parser.add_argument("logs", nargs="*", help="Run logs (default: chat_run_*.json here)")
    parser.add_argument("--out", default="qualtrics_chat_transcript.xlsx")
    args = parser.parse_args()

    paths = args.logs or sorted(glob.glob("chat_run_*.json"))
    if not paths:
        print("No run logs found. Run the agent first, or pass log paths.")
        sys.exit(1)
    build_workbook(paths, args.out)


if __name__ == "__main__":
    main()
