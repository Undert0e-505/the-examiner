"""
src/email.py - render the per-assessment email to outbox/<batch>.txt

Like publish.py, this is a small, self-contained renderer. It
reads the marked assessment output (SUMMARY.md + Q*.marking.md)
plus private/student.json, and emits a plain-text email body
to outbox/<batch>.txt.

The script does NOT send the email. The user (Aaron) reviews the
text, then sends it from Gmail or another client. This is the
"trigger email manually" the user asked for. A future commit can
add Gmail API sending; for now, the staging-vs-live email flip
in private/student.json is sufficient for the manual flow.

The recipient_email_staging field is required. If it's still the
placeholder, the script refuses to run with a clear error
pointing to the file to edit.

Usage:

    D:\\Python310\\python.exe src/email.py ^        --batch aqa-84621h-chemistry-higher-2024-05 ^        --yes

    # to the live recipient (the student) instead of staging:
    D:\\Python310\\python.exe src/email.py ^        --batch <slug> ^        --to live ^        --yes

    # preview only (no file written):
    D:\\Python310\\python.exe src/email.py --batch <slug> --dry-run

The text format is plain text (no HTML email), which is the
professional default and renders on every client.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from publish import (
    REPO_ROOT,
    read_student_json, parse_summary, parse_question_marking,
)

OUTBOX = REPO_ROOT / "outbox"


def recipient(student: dict, mode: str) -> str:
    """Return the recipient email based on the --to flag.

    The active identity's `recipient_email_staging` and
    `recipient_email_live` fields are what we read. When the
    active identity is 'aaron' (default), both fields point at
    Aaron's email, so --to staging and --to live are equivalent
    — both go to Aaron. When the active identity is 'will',
    staging goes to Aaron (safety) and live goes to the student.
    """
    if mode == "staging":
        return student.get("recipient_email_staging", "")
    if mode == "live":
        return student.get("recipient_email_live", "")
    raise ValueError(f"unknown --to mode: {mode!r}; expected 'staging' or 'live'")


def subject_for(slug: str, summary: dict) -> str:
    total_a = summary.get("total_awarded", 0)
    total_p = summary.get("total_available", 0)
    pct = round(100 * total_a / total_p) if total_p else 0
    # Compact, scannable subject line. The slug is a content id, not
    # user-facing. Use the paper title from pair.json instead.
    return f"Your {summary.get('paper_code', 'GCSE')} result is ready — {total_a}/{total_p} ({pct}%)"


def render_email_body(slug: str, summary: dict, questions: list[dict], student: dict, public_url: str, mode: str) -> str:
    """Plain-text email. No markdown (most clients render it
    inconsistently). Tight prose, scannable, professional."""
    if mode == "staging":
        salutation = "Hi Aaron"
        first = ""
    else:
        salutation = student.get("salutation_for_email", "Hey")
        first = student.get("first_name_for_salutation", "")
    signoff = student.get("signoff_for_email", "")
    total_a = summary.get("total_awarded", 0)
    total_p = summary.get("total_available", 0)
    pct = round(100 * total_a / total_p) if total_p else 0

    # Per-Q tally
    q_lines = []
    for q in questions:
        qa = q.get("total_awarded", 0)
        qp = q.get("total_available", 0)
        qmark = "  " if qa == qp else ("+" if qa > 0.5 * qp else "-")
        q_lines.append(f"  {q['q_label']:<6} {qa:>3}/{qp:<3}   {qmark}")

    q_table = "\n".join(q_lines)

    # Where did the marks go? Pull the strongest 2-3 observations
    # from the SUMMARY (skip the OCR-blockers / IDK-pattern paragraphs
    # which are too operational for the student).
    obs = summary.get("observations_md", "").strip()
    obs_short = ""
    if obs:
        # Take the first 2 paragraphs only, capped to 600 chars
        paragraphs = [p for p in obs.split("\n\n") if p.strip()][:2]
        # Strip markdown for plain-text rendering
        obs_text = "\n\n".join(paragraphs)
        obs_text = re.sub(r"\*\*([^*]+)\*\*", r"\1", obs_text)   # bold
        obs_text = re.sub(r"`([^`]+)`", r"\1", obs_text)        # inline code
        if len(obs_text) > 800:
            obs_text = obs_text[:800].rsplit(".", 1)[0] + "..."
        obs_short = obs_text

    if first:
        salutation = f"{salutation} {first}"

    body = f"""{salutation},

Your {summary.get('paper_code', 'GCSE paper')} has been marked.

Total: {total_a}/{total_p} ({pct}%)

Per-question:
{q_table}

View the per-criterion breakdown on the assessment page:
{public_url}

{obs_short}

The page lets you agree or disagree with each mark, or add a
note about what you wrote on the page. The notes go into the
calibration log so future marks reflect what you've shown
you can do.

{signoff}
"""
    return body


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render the per-assessment email to outbox/<batch>.txt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--batch", required=True, help="Paper slug (e.g. aqa-84621h-chemistry-higher-2024-05).")
    p.add_argument("--to", choices=("staging", "live"), default="staging",
                   help="Which recipient_email_* from private/student.json to use. Default staging (Aaron).")
    p.add_argument("--site", default="https://undert0e-505.github.io/the-examiner",
                   help="Base URL of the public Pages site. Default is the live URL.")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    p.add_argument("--dry-run", action="store_true", help="Don't write the file. Print the body.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    student = read_student_json(require_recipient=True)

    print(f"Active identity: {student['_active_identity']!r}  (from private/active.json)")
    print(f"Display name:    {student['display_name']!r}")

    slug = args.batch
    summary = parse_summary(slug)
    questions = []
    for q_row in summary.get("q_rows", []):
        q_short = q_row["q"]
        q_padded = "Q" + q_short.lstrip("Q").zfill(2)
        q = parse_question_marking(slug, q_padded)
        if q is not None:
            questions.append(q)
    questions.sort(key=lambda q: int(q["qnum"]))

    public_url = f"{args.site.rstrip('/')}/assessments/{slug}.html"
    subj = subject_for(slug, summary)
    body = render_email_body(slug, summary, questions, student, public_url, args.to)
    to = recipient(student, args.to)

    if args.dry_run:
        print(f"To: {to}")
        print(f"Subject: {subj}")
        print("-" * 60)
        print(body)
        return 0

    OUTBOX.mkdir(parents=True, exist_ok=True)
    out = OUTBOX / f"{slug}.txt"
    if out.exists() and not args.yes:
        print(f"{out} already exists. Re-run with --yes to overwrite.")
        return 1

    out.write_text(
        f"To: {to}\n"
        f"Subject: {subj}\n"
        f"\n"
        f"{body}",
        encoding="utf-8",
    )
    print(f"  wrote {out}  ({len(body)} bytes body)")
    print(f"  To:      {to}")
    print(f"  Subject: {subj}")
    print()
    print("Next step: review the file, then send from Gmail (or whichever client).")
    print("To switch from staging (Aaron) to live (student), re-run with --to live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
