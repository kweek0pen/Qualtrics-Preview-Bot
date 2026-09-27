#!/usr/bin/env python3
"""
Overall opinion of each Emma Steel conversation, as a researcher would jot it
down after reading the transcript: was the chatbot aggressive, did it listen,
did it stay on topic, did it wrap up properly, and so on.

The review is written once into the run log ("conversation_review") so the
workbook can be rebuilt any number of times without calling the model again.

USAGE:
    python conversation_review.py                  # every run in runs/aggregated_runs.txt
    python conversation_review.py run1.json ...    # specific logs
    python conversation_review.py --redo           # replace existing reviews
"""

import argparse
import json
import sys
from pathlib import Path

from qualtrics_preview_agent import _chat_json

REVIEW_KEY = "conversation_review"


def _transcript_text(chat):
    return "\n\n".join(
        f"{'PARTICIPANT' if m.get('speaker') == 'participant' else 'EMMA'} "
        f"(turn {m.get('turn', '')}): {m.get('text', '')}"
        for m in chat
    )


def review_conversation(client, model, run):
    """A few plain sentences on how the conversation went, or "" when there
    was no conversation or the model gave nothing usable."""
    chat = run.get("chat", [])
    if not chat:
        return ""
    prompt = (
        "You are a research assistant reviewing a transcript from a study. Emma Steel "
        "is a chatbot told to argue against the participant's political opinion. "
        "Read the conversation and write your overall opinion of it for the research "
        "team, in 3 to 5 plain sentences.\n\n"
        "Comment on what stands out, for example: whether Emma was aggressive, "
        "condescending, dismissive, or respectful; whether it actually engaged with "
        "what the participant said or repeated itself; whether it stayed on the "
        "assigned topic; whether its replies were too long or too hard to read; "
        "whether the participant sounded like a real person; and whether the "
        "conversation ended naturally or broke off.\n\n"
        f"PARTICIPANT'S OPINION:\n{run.get('opinion_statement', '')}\n\n"
        f"CONVERSATION:\n{_transcript_text(chat)}\n\n"
        "Return ONLY JSON: {\"review\": \"<your overall opinion>\"}"
    )
    result = _chat_json(client, model, prompt, temperature=0.3)
    text = result.get("review") if isinstance(result, dict) else None
    return (text or "").strip()


def review_log(client, model, log_file, redo=False):
    """Add a review to one run log in place. Returns the review text."""
    path = Path(log_file)
    with open(path, "r") as f:
        run = json.load(f)
    if run.get(REVIEW_KEY) and not redo:
        return run[REVIEW_KEY]
    review = review_conversation(client, model, run)
    if review:
        run[REVIEW_KEY] = review
        with open(path, "w") as f:
            json.dump(run, f, indent=2, ensure_ascii=False)
    return review


def main():
    parser = argparse.ArgumentParser(description="Review Emma Steel conversations.")
    parser.add_argument("logs", nargs="*",
                        help="Run logs (default: every run in runs/aggregated_runs.txt)")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--redo", action="store_true", help="Replace existing reviews")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    paths = args.logs
    if not paths:
        listing = here / "runs" / "aggregated_runs.txt"
        if not listing.exists():
            print("No run logs found. Pass log paths.")
            sys.exit(1)
        paths = [str(here / k) for k in listing.read_text().split() if (here / k).exists()]

    from openai import OpenAI
    client = OpenAI()
    for p in paths:
        review = review_log(client, args.model, p, redo=args.redo)
        print(f"{Path(p).name}: {'reviewed' if review else 'no review (no chat?)'}")


if __name__ == "__main__":
    main()
