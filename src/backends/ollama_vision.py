"""
src/backends/ollama_vision.py Ã¢â‚¬â€ Ollama vision adapter for OCR and marking.

Replaces the Codex path for the-examiner. Instead of spawning Codex
sandboxes, we call Ollama's /api/chat directly with the rendered prompt
and the page photos as base64 image attachments. The model reads the
photos and returns text; we parse the text into the expected output
files and write them to disk.

This module handles two operations:
  1. run_ocr(slug, page_numbers, ...) -> writes *.transcript.md files
  2. run_marking(slug, ...) -> writes Q*.marking.md + SUMMARY.md files

Both use the same Ollama model (default: minimax-m3:cloud, which supports
image input via the Ollama API).

The prompt templates (src/prompts/ocr.md.j2 and src/prompts/mark.md.j2)
are designed for Codex (file-system access, multi-turn). For Ollama we
render the same templates but strip the Codex-specific framing (sandbox,
file paths, "write files to disk" instructions) and replace with a
simpler instruction: "Return all transcript/marking sections as a single
text response with clear section delimiters."

The output parser splits the model's response by the section delimiters
and writes each section to its own file, matching the Codex path's output
shape so downstream consumers (publish.py, mark_batch parsers) work
unchanged.
"""
from __future__ import annotations

import base64
import json
import re
import time
import random
from pathlib import Path
from typing import Any, Iterable

import requests

from backends import ollama_m3

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:397b-cloud"
DEFAULT_TIMEOUT = 600.0

REPO_ROOT = Path("D:/dev/the-examiner")


# ---------------------------------------------------------------------------
# Low-level Ollama call with images
# ---------------------------------------------------------------------------

def _b64_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def chat_with_images(
    user_text: str,
    image_paths: Iterable[Path],
    *,
    system: str = "",
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 3,
) -> str:
    """Send a user prompt + N image attachments to Ollama, return the
    assistant's text content."""
    return ollama_m3.chat_with_images(
        user_text,
        image_paths,
        system=system,
        model=model,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
    )


# ---------------------------------------------------------------------------
# OCR via Ollama
# ---------------------------------------------------------------------------

OCR_SYSTEM = """You are an OCR assistant. You read photos of a printed GCSE
exam paper and transcribe the handwritten student answers verbatim. You must
be faithful to the original Ã¢â‚¬â€ do not correct spelling, grammar, or arithmetic.
You are not marking the paper, just transcribing what the student wrote."""

OCR_USER_PREAMBLE = """You will be shown {n} photos of a GCSE exam paper.
For each photo, produce a transcript section following the format below.

## Output format

For EACH photo, start the section with a delimiter line:

=== PAGE {page_num} ===

Then write the transcript with these three sections:

### 1. Page identification (4-6 lines)
- Paper code at bottom of page
- Page number at top of page
- Question numbers visible
- Total marks available on page
- One sentence describing the printed context

### 2. Verbatim transcript of the student's handwritten answers
For each answer space, transcribe the handwriting verbatim. Use `### Q... (N marks)`
headers. Conventions:
- Write literal words including spelling mistakes. Do NOT correct.
- For chemistry symbols/equations: use inline code with backticks.
- For ticked MCQ options: write `Ticked: <letter>`.
- For illegible words: write `[illegible]`.
- For empty answer spaces: write `(no answer written)`.
- For crossed-out words: write the original word followed by `(crossed out)`.
- For diagrams: describe in 1-2 sentences in brackets.
- For graphs: report number of plotted points, approximate coordinates,
  equation and shape of any drawn line, and coordinates of any intersection.

After the verbatim transcript, add: `*Reading note:* <confidence description>`

### 3. Per-page verdict (one line)
`*Verdict:* easy | medium | hard`

## Special case: page 1 (cover page)
Page 1 is the COVER PAGE Ã¢â‚¬â€ no handwritten answers expected. Keep the
transcript short: page identification + one paragraph describing the cover
contents + the verdict line. Do not invent handwriting.

## Critical rules
- Do NOT rewrite or correct the student's work. Verbatim means verbatim.
- If you cannot read a word, write `[illegible]`. Do not guess.
- Each section MUST start with `=== PAGE N ===` on its own line.
- Write ALL {n} sections in a single response.
"""


def run_ocr(
    slug: str,
    page_numbers: list[int],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    skip_staging: bool = True,
) -> dict:
    """Run OCR on all photos in intake/<slug>/ using Ollama vision.

    Writes *.transcript.md files to intake/<slug>/NN.transcript.md.

    Returns a dict matching the Codex path's return shape:
    {
        "intake_dir": Path,
        "copied_photos": list[Path],
        "prompt_file": Path | None,
        "codex_returncode": int,  # 0 on success, non-zero on failure
        "codex_err_tail": str,
        "transcripts_copied_back": list[Path] | None,
    }
    """
    intake_dir = REPO_ROOT / "intake" / slug
    if not intake_dir.is_dir():
        raise FileNotFoundError(
            f"Intake dir not found at {intake_dir}. Stage photos first."
        )

    photos = sorted(intake_dir.glob("*.jpg"))
    if not photos:
        raise FileNotFoundError(
            f"No .jpg files in {intake_dir}. Stage photos first."
        )

    print(f"[ollama-vision] OCR: {len(photos)} photos in {intake_dir}", flush=True)
    print(f"[ollama-vision] OCR: model={model}, timeout={timeout:.0f}s", flush=True)

    # Build the prompt Ã¢â‚¬â€ one call with all photos
    user_text = OCR_USER_PREAMBLE.format(n=len(photos), page_num=page_numbers)

    t0 = time.time()
    try:
        response = chat_with_images(
            user_text,
            photos,
            system=OCR_SYSTEM,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
    except Exception as e:
        elapsed = time.time() - t0
        return {
            "intake_dir": intake_dir,
            "copied_photos": photos,
            "prompt_file": None,
            "codex_returncode": 1,
            "codex_err_tail": f"Ollama OCR failed after {elapsed:.1f}s: {e}",
            "transcripts_copied_back": None,
        }
    elapsed = time.time() - t0
    print(f"[ollama-vision] OCR: response in {elapsed:.1f}s ({len(response)} chars)", flush=True)

    # Parse the response into per-page transcripts
    transcripts = parse_ocr_response(response, page_numbers)
    written = []
    for page_num, content in transcripts.items():
        out_path = intake_dir / f"{page_num:02d}.transcript.md"
        out_path.write_text(content, encoding="utf-8")
        written.append(out_path)
        print(f"[ollama-vision] OCR: wrote {out_path} ({len(content)} chars)", flush=True)

    if not written:
        return {
            "intake_dir": intake_dir,
            "copied_photos": photos,
            "prompt_file": None,
            "codex_returncode": 1,
            "codex_err_tail": f"Ollama OCR returned no parseable sections. Response starts: {response[:500]}",
            "transcripts_copied_back": None,
        }

    return {
        "intake_dir": intake_dir,
        "copied_photos": photos,
        "prompt_file": None,
        "codex_returncode": 0,
        "codex_err_tail": "",
        "transcripts_copied_back": written,
    }


def parse_ocr_response(response: str, page_numbers: list[int]) -> dict[int, str]:
    """Parse the model's response into per-page transcript strings.

    Expected delimiter: `=== PAGE N ===` on its own line.
    Returns dict mapping page_num -> transcript text.
    """
    # Split on the delimiter
    pattern = r"^=== PAGE (\d+) ===\s*$"
    sections: dict[int, str] = {}
    current_page = None
    current_lines: list[str] = []

    for line in response.splitlines():
        m = re.match(pattern, line.strip())
        if m:
            if current_page is not None:
                sections[current_page] = "\n".join(current_lines).strip()
            current_page = int(m.group(1))
            current_lines = []
        elif current_page is not None:
            current_lines.append(line)

    if current_page is not None:
        sections[current_page] = "\n".join(current_lines).strip()

    # If no delimiters found, try to treat the whole response as page 1
    if not sections and len(page_numbers) == 1:
        sections[page_numbers[0]] = response.strip()

    return sections


# ---------------------------------------------------------------------------
# Marking via Ollama
# ---------------------------------------------------------------------------

MARKING_SYSTEM = """You are a GCSE examiner. You compare a student's
transcribed answers against the official mark scheme and award marks per
criterion. You are strict but fair: you apply the mark scheme as written,
award process marks where the scheme allows, and do not give benefit of
the doubt beyond what the scheme permits."""

MARKING_USER_PREAMBLE = """You are marking a GCSE exam paper. Below are the
student's transcripts (one per page) and the full mark scheme in JSON.

## Your task

For each question group Q1-Q{question_count}, produce a marking file section.
Then produce a summary section. Use the exact delimiters below.

## Output format — STRICT, do not deviate

For each question Q-NN, start with:

=== Q{q_num:02d} ===

Then write EXACTLY this structure (in this order):

### 1. Question identification

```
Total marks available: <integer>
Question sub-parts covered by the transcripts: <e.g. Q01.1, Q01.2, Q01.3, Q01.4, Q01.5, Q01.6>
Printed-context summary: <one sentence describing what the question asks>
```

### 2. Per-criterion marking

For EACH criterion in this question, write a block starting with:

```
### Criterion N: <AO> -- <marks> mark(s)
```

Use exactly TWO ASCII hyphens `--` (NOT em-dash, NOT en-dash).

Each block must have these fields with **bold** labels:

```
**Sub-question this criterion applies to:** <Q-NN.X>
**Indicative content:** <bullet list from the markscheme, prefixed with "- ">
**Transcript section covered:** <NN.transcript.md, Q-NN.X — or "not covered by any transcript">
**Decision:** AWARD | NOT_AWARD | NOT_APPLICABLE
**Marks awarded:** <integer 0 to marks available>
**Justification:** <2-4 sentences quoting the student's answer where relevant>
```

### 3. Legibility assessment

Write a block with EXACTLY these bold-label fields (no prose, no other format):

```
### Legibility

**legibility_score:** <integer 0-5>
**ocr_mode:** <one of: clear_read | minor_uncertainty | context_inferred | unreadable>
**reason:** <one short sentence>
**student_feedback:** <one short sentence, second person "Your handwriting...">
```

### 4. Question summary

Use EXACTLY this header (H2, no number):

```
## Question Summary

**Total marks awarded for this question:** <integer> out of <integer>
**What cost the most marks:** <one sentence>
**Legibility summary:** <one short sentence>
```

After all {question_count} questions, write:

=== SUMMARY ===

Then write EXACTLY this structure:

```
Paper code: <e.g. 8462/1H>
Sitting: <e.g. Friday 17 May 2024>
Total marks available: {total_marks}
Total marks awarded: <integer>
```

Then a tally table with EXACTLY this format (4 columns, Q number first, no topic column):

```
| Q1 | <available> | <awarded> | <one-sentence notes> |
| Q2 | <available> | <awarded> | <one-sentence notes> |
...
```

Then a section with EXACTLY this header:

```
## Cross-paper observations

<3-5 short paragraphs, student-facing, second person>
```

Then a section with EXACTLY this header:

```
## Assessor notes

<pipeline meta: OCR blockers, marking uncertainty, pipeline verdict>
```

## Mark scheme (JSON)

{markscheme_json}

## Transcripts

{transcripts_text}

## Critical formatting rules

- Use exactly TWO ASCII hyphens `--` in criterion headers. NOT em-dash.
- Each criterion block starts with `### Criterion N: <ao> -- <marks> mark(s)`
- Use `**Field:**` pattern for all fields
- Decision values: AWARD | NOT_AWARD | NOT_APPLICABLE (uppercase, underscore)
- Legibility fields MUST use the exact bold labels: `**legibility_score:**`,
  `**ocr_mode:**`, `**reason:**`, `**student_feedback:**`
- Question summary header MUST be `## Question Summary` (H2, no number)
- SUMMARY.md totals MUST use the format `Total marks available: N` and
  `Total marks awarded: N` (no bold, no asterisks)
- SUMMARY.md tally table MUST have columns: Q | available | awarded | notes
- Write all {question_count} question sections plus the summary section.
- Each section MUST start with its `=== ... ===` delimiter.
"""

def run_marking(
    slug: str,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 900.0,
) -> dict:
    """Run marking via Ollama. Reads transcripts from intake/<slug>/*.transcript.md
    and markscheme from papers/<slug>/markscheme.json. Writes output to
    assessments/<slug>/Q*.marking.md and assessments/<slug>/SUMMARY.md.

    Returns a dict matching the Codex path's return shape:
    {
        "intake_dir": Path,
        "markscheme_path": Path,
        "transcripts": list[Path],
        "prompt_file": Path | None,
        "codex_returncode": int,
        "codex_err_tail": str,
        "marking_files_copied_back": list[Path] | None,
        "tally": dict | None,
    }
    """
    intake_dir = REPO_ROOT / "intake" / slug
    markscheme_path = REPO_ROOT / "papers" / slug / "markscheme.json"
    assessments_dir = REPO_ROOT / "assessments" / slug

    if not markscheme_path.is_file():
        raise FileNotFoundError(f"Markscheme not found at {markscheme_path}")

    transcripts = sorted(intake_dir.glob("*.transcript.md"))
    if not transcripts:
        raise FileNotFoundError(f"No transcripts found in {intake_dir}")

    # Load markscheme
    ms = json.loads(markscheme_path.read_text(encoding="utf-8"))
    marks = ms.get("marks", [])
    question_count = len(marks)

    # Build transcripts text
    transcript_parts = []
    for t in transcripts:
        page_num = t.stem.replace(".transcript", "")
        content = t.read_text(encoding="utf-8")
        transcript_parts.append(f"--- Transcript for page {page_num} ---\n{content}")
    transcripts_text = "\n\n".join(transcript_parts)

    # Build markscheme JSON (trimmed to keep prompt manageable)
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
            for q in marks
        ],
    }
    markscheme_json = json.dumps(ms_trimmed, indent=2, ensure_ascii=False)

    # Build the prompt
    total_marks = sum(q.get("total_marks_for_question", 0) for q in marks)
    user_text = MARKING_USER_PREAMBLE.format(
        question_count=question_count,
        q_num=0,  # placeholder, not used in preamble
        total_marks=total_marks,
        markscheme_json=markscheme_json,
        transcripts_text=transcripts_text,
    )

    print(f"[ollama-vision] Marking: {question_count} questions, {len(transcripts)} transcripts", flush=True)
    print(f"[ollama-vision] Marking: model={model}, timeout={timeout:.0f}s", flush=True)
    print(f"[ollama-vision] Marking: prompt size={len(user_text)} chars", flush=True)

    t0 = time.time()
    try:
        response = chat_with_images(
            user_text,
            [],  # no images for marking Ã¢â‚¬â€ transcripts are text
            system=MARKING_SYSTEM,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
    except Exception as e:
        elapsed = time.time() - t0
        return {
            "intake_dir": intake_dir,
            "markscheme_path": markscheme_path,
            "transcripts": transcripts,
            "prompt_file": None,
            "codex_returncode": 1,
            "codex_err_tail": f"Ollama marking failed after {elapsed:.1f}s: {e}",
            "marking_files_copied_back": None,
            "tally": None,
        }
    elapsed = time.time() - t0
    print(f"[ollama-vision] Marking: response in {elapsed:.1f}s ({len(response)} chars)", flush=True)

    # Parse response into per-question files + summary
    sections = parse_marking_response(response)

    assessments_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for q_num_str, content in sections.items():
        if q_num_str.upper() == "SUMMARY":
            out_path = assessments_dir / "SUMMARY.md"
        else:
            q_num = int(q_num_str[1:])  # strip 'Q' prefix
            out_path = assessments_dir / f"Q{q_num:02d}.marking.md"
        out_path.write_text(content, encoding="utf-8")
        written.append(out_path)
        print(f"[ollama-vision] Marking: wrote {out_path} ({len(content)} chars)", flush=True)

    if not written:
        return {
            "intake_dir": intake_dir,
            "markscheme_path": markscheme_path,
            "transcripts": transcripts,
            "prompt_file": None,
            "codex_returncode": 1,
            "codex_err_tail": f"Ollama marking returned no parseable sections. Response starts: {response[:500]}",
            "marking_files_copied_back": None,
            "tally": None,
        }

    # Try to parse tally from summary
    tally = None
    summary_path = assessments_dir / "SUMMARY.md"
    if summary_path.is_file():
        try:
            from mark_batch import parse_marks_from_summary
            tally = parse_marks_from_summary(summary_path)
        except Exception:
            pass

    return {
        "intake_dir": intake_dir,
        "markscheme_path": markscheme_path,
        "transcripts": transcripts,
        "prompt_file": None,
        "codex_returncode": 0,
        "codex_err_tail": "",
        "marking_files_copied_back": written,
        "tally": tally,
    }


def parse_marking_response(response: str) -> dict[str, str]:
    """Parse the model's response into per-question sections.

    Expected delimiters: `=== Q01 ===`, `=== Q02 ===`, ..., `=== SUMMARY ===`
    Returns dict mapping question number string -> section content.
    """
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
            # Normalise Q01 -> Q1, Q001 -> Q1
            if current_key.startswith("Q"):
                current_key = f"Q{int(current_key[1:])}"
            current_lines = []
        elif current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections