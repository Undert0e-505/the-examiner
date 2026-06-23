"""
src-englit/backends/englit_marking.py — Marking for English Lit papers.

Reads answer transcripts from intake/<slug>/answer-*.transcript.md
and markscheme from papers/<slug>/markscheme.json. Only marks
questions that have answers (as determined by the discovery step).

Writes output to assessments/<slug>/Q*.marking.md and SUMMARY.md,
same format as chemistry so publish.py works unchanged.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from backends.ollama_m3 import chat_with_images

DEFAULT_MODEL = "qwen3.5:397b-cloud"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 900.0

REPO_ROOT = Path("D:/dev/the-examiner")


MARKING_SYSTEM = """You are a GCSE English Literature examiner. You compare a
student's transcribed essay answer against the official mark scheme and award
marks per criterion. You are strict but fair: you apply the mark scheme as
written, award marks for valid points, and do not give benefit of the doubt
beyond what the scheme permits. You understand that student answers may contain
spelling errors, grammar errors, and unclear phrasing — you judge the content,
not the presentation."""

MARKING_USER_TEMPLATE = """You are marking a GCSE English Literature exam paper.
The student has answered {answered_count} question(s) out of {total_questions}
available. Below are the student's transcripts and the relevant mark scheme
criteria in JSON.

## Your task

For each ANSWERED question, produce a marking file section. Then produce a
summary section. Use the exact delimiters below.

## Output format — STRICT, do not deviate

For each answered question Q-NN, start with:

=== Q{q_num:02d} ===

Then write these four sections in this order. Do NOT wrap any section in
triple-backtick code fences. Write all content as plain markdown text.

### 1. Question identification

Write these three lines as plain text (no code fences, no bold):

Total marks available: <integer>
Question sub-parts covered by the transcripts: <e.g. Q08.1, Q08.2>
Printed-context summary: <one sentence describing what the question asks>

### 2. Per-criterion marking

For EACH criterion in this question, write a block. The header line must be:

### Criterion N: <AO> -- <marks> mark(s)

Use exactly TWO ASCII hyphens -- (NOT em-dash).

Each block must have these fields with bold labels:

**Sub-question this criterion applies to:** <Q-NN.X>
**Indicative content:** <bullet list from the markscheme, each prefixed with "- ">
**Transcript section covered:** <which part of the transcript addresses this criterion>
**Decision:** AWARD | NOT_AWARD | NOT_APPLICABLE
**Marks awarded:** <integer 0 to marks available>
**Justification:** <2-4 sentences quoting the student's answer where relevant>

### 3. Legibility assessment

Write a header line:

### Legibility

Then these four fields with bold labels:

**legibility_score:** <integer 0-5>
**ocr_mode:** <one of: clear_read | minor_uncertainty | context_inferred | unreadable>
**reason:** <one short sentence>
**student_feedback:** <one short sentence, second person "Your handwriting...">

### 4. Question summary

Write this header (H2, no number):

## Question Summary

Then three lines as plain text:

**Total marks awarded for this question:** <integer> out of <integer>
**What cost the most marks:** <one sentence>
**Legibility summary:** <one short sentence>

After all answered questions, write:

=== SUMMARY ===

Then write these lines as plain text:

Paper code: <e.g. 8702/1>
Sitting: <e.g. Monday 13 May 2024>
Total marks available: {paper_total_marks}
Total marks awarded: <integer — sum of all answered questions' awarded marks>

Then a tally table as a plain markdown table with EXACTLY four columns:

| Q1 | <available> | <awarded> | <one-sentence notes> |

Only include rows for ANSWERED questions. Unanswered questions should NOT
appear in the table.

Then a section with this header:

## Cross-paper observations

<2-4 short paragraphs, student-facing, second person "You...">

Then a section with this header:

## Assessor notes

<pipeline meta: OCR blockers, marking uncertainty, which questions were answered>

## Mark scheme (JSON — only for answered questions)

{markscheme_json}

## Transcripts

{transcripts_text}

## Critical formatting rules

- Do NOT wrap any output in triple-backtick code fences. All content is plain markdown.
- Use exactly TWO ASCII hyphens -- in criterion headers.
- Each criterion block starts with ### Criterion N: <ao> -- <marks> mark(s)
- Use **Field:** pattern for all fields
- Decision values: AWARD | NOT_AWARD | NOT_APPLICABLE (uppercase, underscore)
- Legibility fields MUST use the exact bold labels
- Question summary header MUST be ## Question Summary (H2, no number)
- SUMMARY totals MUST be plain text: Total marks available: N and Total marks awarded: N
- SUMMARY.md tally table MUST have columns: Q | available | awarded | notes
- Only include ANSWERED questions in all sections
- Each section MUST start with its === ... === delimiter.
"""


def run_marking(
    slug: str,
    answered_questions: list[str],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Run marking for answered questions only.

    Reads transcripts from intake/<slug>/answer-*.transcript.md (written
    by the discovery step) and markscheme from papers/<slug>/markscheme.json.
    Writes Q*.marking.md and SUMMARY.md to assessments/<slug>/.
    """
    intake_dir = REPO_ROOT / "intake" / slug
    markscheme_path = REPO_ROOT / "papers" / slug / "markscheme.json"
    paper_json_path = REPO_ROOT / "papers" / slug / "paper.json"
    assessments_dir = REPO_ROOT / "assessments" / slug

    if not markscheme_path.is_file():
        raise FileNotFoundError(f"Markscheme not found at {markscheme_path}")

    # Load markscheme
    ms = json.loads(markscheme_path.read_text(encoding="utf-8"))
    all_marks = ms.get("marks", [])

    # Load paper.json for total_marks
    paper_total_marks = None
    if paper_json_path.is_file():
        pj = json.loads(paper_json_path.read_text(encoding="utf-8"))
        paper_total_marks = pj.get("total_marks")

    # Filter to only answered questions
    answered_set = set(answered_questions)
    marks_to_mark = [q for q in all_marks if q.get("question_ref") in answered_set]
    question_count = len(marks_to_mark)

    if question_count == 0:
        return {
            "codex_returncode": 1,
            "codex_err_tail": f"No answered questions found. answered_questions={answered_questions}",
            "marking_files_copied_back": None,
            "tally": None,
        }

    # Collect transcripts for answered questions
    transcripts = sorted(intake_dir.glob("answer-*.transcript.md"))
    if not transcripts:
        # Fallback: try *.transcript.md (chemistry naming)
        transcripts = sorted(intake_dir.glob("*.transcript.md"))

    transcript_parts = []
    for t in transcripts:
        content = t.read_text(encoding="utf-8")
        transcript_parts.append(f"--- {t.name} ---\n{content}")
    transcripts_text = "\n\n".join(transcript_parts)

    # Build markscheme JSON (only answered questions)
    ms_trimmed = {
        "schema_version": ms.get("schema_version"),
        "slug": ms.get("slug"),
        "kind": ms.get("kind"),
        "marks": [
            {
                "question_ref": q.get("question_ref"),
                "paper_question_number": q.get("paper_question_number"),
                "total_marks_for_question": q.get("total_marks_for_question"),
                "criteria": q.get("criteria", []),
            }
            for q in marks_to_mark
        ],
    }
    markscheme_json = json.dumps(ms_trimmed, indent=2, ensure_ascii=False)

    # Build the prompt
    user_text = MARKING_USER_TEMPLATE.format(
        answered_count=question_count,
        total_questions=len(all_marks),
        q_num=0,
        paper_total_marks=paper_total_marks or "unknown",
        markscheme_json=markscheme_json,
        transcripts_text=transcripts_text,
    )

    print(f"[englit-marking] Marking: {question_count} answered questions, {len(transcripts)} transcripts", flush=True)
    print(f"[englit-marking] Marking: model={model}, timeout={timeout:.0f}s", flush=True)
    print(f"[englit-marking] Marking: prompt size={len(user_text)} chars", flush=True)

    t0 = time.time()
    try:
        response = chat_with_images(
            user_text,
            [],  # no images for marking — transcripts are text
            system=MARKING_SYSTEM,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
    except Exception as e:
        elapsed = time.time() - t0
        return {
            "codex_returncode": 1,
            "codex_err_tail": f"Marking failed after {elapsed:.1f}s: {e}",
            "marking_files_copied_back": None,
            "tally": None,
        }
    elapsed = time.time() - t0
    print(f"[englit-marking] Marking: response in {elapsed:.1f}s ({len(response)} chars)", flush=True)

    # Parse response into per-question files + summary
    sections = _parse_marking_response(response)

    assessments_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for q_key, content in sections.items():
        if q_key.upper() == "SUMMARY":
            out_path = assessments_dir / "SUMMARY.md"
        else:
            q_num = int(q_key[1:])  # strip 'Q'
            out_path = assessments_dir / f"Q{q_num:02d}.marking.md"
        out_path.write_text(content, encoding="utf-8")
        written.append(out_path)
        print(f"[englit-marking] Marking: wrote {out_path} ({len(content)} chars)", flush=True)

    if not written:
        return {
            "codex_returncode": 1,
            "codex_err_tail": f"No parseable sections. Response starts: {response[:500]}",
            "marking_files_copied_back": None,
            "tally": None,
        }

    return {
        "codex_returncode": 0,
        "codex_err_tail": "",
        "marking_files_copied_back": written,
        "tally": None,
    }


def _parse_marking_response(response: str) -> dict[str, str]:
    """Parse the model's response into per-question sections."""
    pattern = r"^=== (Q\d+|SUMMARY) ===\s*$"
    sections: dict[str, str] = {}
    current_key = None
    current_lines: list[str] = []

    for line in response.splitlines():
        m = re.match(pattern, line.strip(), re.IGNORECASE)
        if m:
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = m.group(1).upper()
            if current_key.startswith("Q"):
                current_key = f"Q{int(current_key[1:])}"
            current_lines = []
        elif current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections


def write_transcripts_from_discovery(
    slug: str,
    match_result: dict,
) -> list[Path]:
    """Write answer transcripts from the discovery match result to disk.

    The match result has an 'answers' list, each with photo_index,
    transcript, matched_question, etc. We write each as
    answer-<qref>-<seq>.transcript.md.
    """
    intake_dir = REPO_ROOT / "intake" / slug
    if not intake_dir.is_dir():
        raise FileNotFoundError(f"Intake dir not found: {intake_dir}")

    # Group answers by question
    by_question: dict[str, list[dict]] = {}
    for ans in match_result.get("answers", []):
        mq = ans.get("matched_question")
        if mq:
            by_question.setdefault(mq, []).append(ans)

    written = []
    for qref, answers in by_question.items():
        for seq, ans in enumerate(answers, 1):
            transcript = ans.get("transcript", "")
            out_path = intake_dir / f"answer-{qref}-{seq:02d}.transcript.md"
            out_path.write_text(transcript, encoding="utf-8")
            written.append(out_path)
            print(f"[englit-marking] Wrote transcript {out_path.name} ({len(transcript)} chars)", flush=True)

    return written