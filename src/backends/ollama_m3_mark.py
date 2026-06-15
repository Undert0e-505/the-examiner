"""
src/backends/ollama_m3_mark.py — Ollama-m3 marking adapter.

A drop-in alternative to the codex_lane path in src/mark_batch.py.
Instead of one Codex call that produces all 9 question marking
files + SUMMARY.md, we make one chat call per question, asking
the model to compare the relevant transcripts against the
markscheme criteria for that question and produce a
`Q<NN>.marking.md` file.

Public surface:
    mark_question(slug, question_n, criteria, transcripts, ...)
        Returns the marking text for a single question.
    mark_batch(slug, ...) -> list[Path]
        Iterates questions, writes assessments/<slug>/Q<NN>.marking.md
        for each, returns the list of written paths.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from backends.ollama_m3 import chat_with_images, OllamaError

REPO_ROOT = Path("D:/dev/the-examiner-m3")

MARK_SYSTEM = """You are an exam marker for AQA GCSE Chemistry. For one
question, you compare the student's handwritten answers (provided
as OCR transcripts) against the markscheme criteria (provided as
JSON) and produce a per-criterion marking file in a strict format
that a downstream Python parser will read.

The parser is brittle about format. Follow the output spec exactly:
- Use exactly TWO ASCII hyphens `--` (NOT em-dash, NOT en-dash)
  between the AO label and the marks count in the criterion
  header.
- Use exactly TWO ASCII hyphens `--` in the "Sub-question this
  criterion applies to" line.
- Each criterion block must start with `### Criterion` at the
  start of the line. No leading whitespace.
- Each field within a block must start with `**Field:**` (bold
  field name + colon + value). Field names are case-sensitive
  and must match exactly: `Sub-question this criterion applies
  to:`, `Indicative content:`, `Transcript section covered:`, `Decision:`,
  `Marks awarded:`, `Justification:`, `Assessor notes:`.
- The criterion block end is the next blank line followed by a
  line starting with `###` or `####`.
- Use plain ASCII in every header, field-name, and structural
  line. Em-dashes are fine inside the `Justification` text body
  (the parser does not read it), but they must NOT appear in any
  header line, any field-name line, or any `Decision:` /
  `Marks awarded:` line.
- The file must end with `#### 4. Question summary` followed by
  the awarded/total line, a one-sentence narrative, and a
  legibility assessment.

You MUST respond with the marking file contents only. No prose
before or after. Do not wrap in code fences."""


MARK_USER_TEMPLATE = """Mark the student's handwritten answers to
**Question {q_padded}** of the GCSE exam paper `{slug}`
(total marks: {q_marks}).

## Markscheme criteria for this question

```json
{criteria_json}
```

## Student's transcripts (verbatim OCR, possibly with reading notes)

{transcripts_block}

## What to produce

A single marking file in EXACTLY this shape (replace the
placeholder values; do not include the angle brackets):

```
#### 1. Question identification

- Question number: {q_int}
- Total marks available: {q_marks}
- Question sub-parts covered by the transcripts: <comma-separated sub-question IDs, e.g. Q01.1, Q01.2>
- Printed-context summary: <one sentence>

#### 2. Per-criterion marking

### Criterion 1: AO<ao> -- <marks> mark(s)
**Sub-question this criterion applies to:** <sub-question id, e.g. Q01.1>
**Indicative content:** <space-separated list of acceptable answer points from the markscheme>
**Transcript section covered:** <NN>.transcript.md, <sub-question id>
**Decision:** AWARD or NOT_AWARD
**Marks awarded:** <integer 0..marks>
**Justification:** <one to three sentences comparing the student's answer to the indicative content>
**Assessor notes:** <None or a short note>

### Criterion 2: AO<ao> -- <marks> mark(s)
... etc, one block per criterion in the markscheme above ...

#### 3. Legibility assessment

### Legibility

**legibility_score:** <integer 1..5>
**ocr_mode:** <clear_read | minor_guess | recheck_needed>
**reason:** <one short sentence>
**student_feedback:** <one short sentence in second person, e.g. "Your handwriting was clear; nothing to change.">

#### 4. Question summary

- **Total marks awarded for this question:** <integer> out of {q_marks}.
- <one to three sentences summarising strengths and gaps>.
- **Legibility summary:** Question {q_int} was <easy/medium/hard> to read (score <N>/5), with <specific note>.
```

## Rules (PARSE-CRITICAL)

- Use exactly TWO ASCII hyphens `--` between the AO label and
  the marks count in EVERY criterion header. Em-dash `—` or
  en-dash `–` will be silently dropped by the parser.
- Use exactly TWO ASCII hyphens `--` in the "Sub-question this
  criterion applies to" line.
- Each criterion block must start with `### Criterion` at the
  start of the line. No leading whitespace.
- Every field starts with `**Field:**` where Field is one of
  the EXACT field names listed above. Case-sensitive.
- `Decision:` must be exactly `AWARD` or `NOT_AWARD` (no quotes,
  no extra words).
- `Marks awarded:` must be an integer 0..marks.
- `Justification:` should reference the specific words the
  student wrote (e.g. "The student wrote `copper sulphate` ...")
  so a human reading the published page can follow the
  reasoning.
- `Assessor notes:` should be `None.` if there is nothing to
  flag; otherwise one short sentence.
- Do NOT wrap the output in ``` fences. Just raw markdown.
- The file must end with the legibility summary line.
"""


def _format_criteria(criteria: list[dict]) -> str:
    """Return the markscheme criteria as a pretty-printed JSON string
    the model can read."""
    return json.dumps(criteria, indent=2, ensure_ascii=False)


def _format_transcripts(transcripts: list[tuple[int, str]]) -> str:
    """Format the (page_num, transcript_text) pairs as a labelled block
    the model can read."""
    out = []
    for page_num, text in transcripts:
        out.append(f"\n--- Page {page_num:02d} ---\n{text}\n")
    return "".join(out)


def mark_question(
    slug: str,
    question_n: int,
    criteria: list[dict],
    transcripts: list[tuple[int, str]],
    *,
    model: str = "minimax-m3:cloud",
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 300.0,
) -> str:
    """Call Ollama-m3 with the markscheme criteria + relevant
    transcripts and return the marking file text."""
    q_padded = f"{question_n:02d}"
    q_marks = sum(c.get("marks", 0) for c in criteria)
    user_text = MARK_USER_TEMPLATE.format(
        q_padded=q_padded,
        q_int=question_n,
        q_marks=q_marks,
        slug=slug,
        criteria_json=_format_criteria(criteria),
        transcripts_block=_format_transcripts(transcripts),
    )
    return chat_with_images(
        user_text,
        [],  # no images for marking: the model already has the OCR
        system=MARK_SYSTEM,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )


def _strip_code_fences(text: str) -> str:
    text = re.sub(r"^\s*```(?:[a-z]+)?\s*\n", "", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"\n```\s*$", "", text, count=1)
    return text


def mark_batch(
    slug: str,
    *,
    questions_to_mark: list[int] | None = None,
    model: str = "minimax-m3:cloud",
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 300.0,
    repo_root: Path = REPO_ROOT,
) -> list[Path]:
    """Run marking for one or more questions. Reads the markscheme
    and the relevant transcripts, writes assessments/<slug>/Q<NN>.marking.md
    for each question, returns the list of written paths.

    Existing marking files at the same path are preserved as *.bak,
    matching the convention in mark_batch.py.
    """
    markscheme_path = repo_root / "papers" / slug / "markscheme.json"
    if not markscheme_path.is_file():
        raise FileNotFoundError(f"markscheme not found: {markscheme_path}")
    markscheme = json.loads(markscheme_path.read_text(encoding="utf-8"))
    intake_dir = repo_root / "intake" / slug
    if not intake_dir.is_dir():
        raise FileNotFoundError(f"intake not found: {intake_dir}")
    assessments_dir = repo_root / "assessments" / slug
    assessments_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for q in markscheme.get("marks", []):
        qn = int(q["paper_question_number"])
        if questions_to_mark is not None and qn not in questions_to_mark:
            continue
        criteria = q.get("criteria", [])
        # Find the pages that cover this question by looking at the
        # transcripts' "Visible questions" line. Conservative: read
        # every transcript that mentions this question's number.
        q_label = f"Q{qn:02d}"
        transcripts: list[tuple[int, str]] = []
        for tf in sorted(intake_dir.glob("*.transcript.md")):
            page_num = int(re.match(r"(\d+)\.transcript\.md", tf.name).group(1))
            text = tf.read_text(encoding="utf-8")
            # Match "Visible questions: Q01.1, Q01.2" or "Question number: 01"
            if (
                f"Q{qn:02d}." in text
                or f"Question {qn}" in text
                or f" {qn:02d}." in text  # older format
            ):
                transcripts.append((page_num, text))
        if not transcripts:
            print(
                f"[ollama-m3] mark Q{qn:02d}: no transcripts mention this question; skipping",
                flush=True,
            )
            continue
        print(
            f"[ollama-m3] mark Q{qn:02d}: "
            f"{len(criteria)} criteria, {len(transcripts)} transcript(s)",
            flush=True,
        )
        text = mark_question(
            slug,
            qn,
            criteria,
            transcripts,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
        text = _strip_code_fences(text)
        dest = assessments_dir / f"Q{qn:02d}.marking.md"
        if dest.exists():
            bak = dest.with_suffix(".marking.md.bak")
            print(f"  backing up {dest.name} -> {bak.name}", flush=True)
            dest.replace(bak)
        dest.write_text(text, encoding="utf-8")
        print(f"  wrote {dest} ({len(text)} chars)", flush=True)
        written.append(dest)
    return written
