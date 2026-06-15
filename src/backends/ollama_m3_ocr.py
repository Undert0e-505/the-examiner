"""
src/backends/ollama_m3_ocr.py — Ollama-m3 OCR adapter for the-examiner.

A drop-in alternative to the codex_lane path in src/ocr_batch.py.
Instead of running Codex in a disposable sandbox that batch-processes
N photos in one chat call, we make N separate chat calls (one per
photo) and write each transcript file directly to the real repo's
intake/<slug>/.

Why per-photo: the OCR prompt is dense (page identification +
verbatim transcript + reading note + verdict). Sending all 26 photos
in a single call with that prompt would overflow the model's
effective context after the system prompt. Per-photo is slower in
wall-time but reliable.

Public surface:
    ocr_photo(photo_path, slug, page_number, page_context=None) -> str
        Returns the transcript text for a single photo.
    ocr_batch(photo_paths, slug, page_numbers, ...) -> list[Path]
        Iterates photos, writes intake/<slug>/<NN>.transcript.md
        for each, returns the list of written paths.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from backends.ollama_m3 import chat_with_images, OllamaError

REPO_ROOT = Path("D:/dev/the-examiner-m3")

OCR_SYSTEM = """You are an OCR/vision assistant for a GCSE exam-paper
marker. For each photo of a printed exam page, you will produce a
transcript file containing: (1) page identification, (2) verbatim
transcript of the student's handwritten answers, (3) a reading
note, (4) a single-line verdict. You MUST NOT invent text that is
not in the photo. Verbatim means verbatim — preserve every spelling
mistake, crossed-out word, and arithmetic step. If you cannot read
a word, write `[illegible]`. For diagrams, describe what is drawn
in brackets (counts, coordinates, shapes); do not interpret as text.
For graphs, report the number of plotted points, their approximate
coordinates, the equation and shape of any drawn line(s), and the
coordinates of any intersection. You are NOT being asked to mark
the work — that is a separate pass. You are only producing the
faithful transcript."""


OCR_USER_TEMPLATE = """Transcribe the handwritten student answers on
**printed page {page}** of the GCSE exam paper `{slug}`. The photo
is attached inline.

## What to produce

Write a transcript file with EXACTLY this structure (no extra
prose, no code fences around the whole thing):

```
Paper code: <e.g. 8462/1H>
Printed page: <number>
Visible questions: <e.g. Q04.1, Q04.2, Q04.3, Q04.4>
Total marks on page: <number>

<one short sentence describing the printed context: the question
wording, any apparatus table, any diagram referenced. Do not
transcribe the printed question word-for-word — a short summary is
fine.>

<For each answer space on the page, in order, transcribe the
handwriting VERBATIM. Wrap each answer in a `### Q... (N marks)`
header.>

<Conventions for the verbatim section:>
- Text: literal words, including spelling mistakes, grammar mistakes,
  and missing punctuation. Do NOT correct to proper English.
- Chemistry symbols, equations, formulas, working: write in inline
  code with backticks, using plain-text approximations
  (e.g. `NH4Cl`, `2Na + Cl2 -> 2NaCl`, `proton number: 12`,
  `nucleon number: 24`). Preserve the line breaks of the working.
- Numbers and arithmetic: write exactly as written, even if wrong.
- Ticked MCQ options: write `Ticked: <letter>`.
- Diagrams or drawings: describe in one or two short sentences in
  brackets. Do not attempt to interpret diagrams as text. For
  graphs: report the number of plotted points, their approximate
  coordinates, the equation and shape of any drawn line(s), and
  the coordinates of any intersection. The marking pass needs
  enough detail to award markscheme points for best-fit lines,
  plotted crosses, and read-off values.
- Ticked statements in a list: write `Ticked: <statement>`.
- Illegible words or numbers: write `[illegible]` and note the
  position. Do not guess.
- Empty answer spaces: write `(no answer written)`.
- Crossed-out words: write the original word followed by
  `(crossed out)`.
- Scribbles / doodles in margins: ignore.

<CRITICAL: DO NOT REWRITE OR CORRECT THE STUDENT'S WORK. This
applies to prose, arithmetic, and chemistry. The marking pass is
a separate step that compares the transcript to the markscheme.>

*Reading note:* <one short sentence describing your confidence. Be
specific about which words or numbers you are sure of and which
you are not. For pages with diagrams or graphs, specifically note
whether the diagram description is detailed enough to mark
against.>

*Verdict:* easy | medium | hard
```

**Special case for the COVER PAGE (page 1)**: this is the exam
cover with the candidate name boxes, the marking grid, the exam
title, the time allowed, and the total marks. The expected output
for this file is a brief description of what the cover page shows,
not a transcript of handwritten answers (there should be no
handwriting on the cover, or only the candidate name if the
student wrote it). Keep this transcript file SHORT — page
identification + a one-paragraph description of the cover's
contents + the verdict line. Do not invent handwriting that isn't
there.
{page_context_line}
Respond with the transcript file contents only. No prose before
or after.
"""


def _format_page_context(page_context: str | None) -> str:
    if page_context:
        return f"\n## Additional context for this page\n\n{page_context}\n"
    return ""


def ocr_photo(
    photo_path: Path,
    slug: str,
    page_number: int,
    page_context: str | None = None,
    *,
    model: str = "minimax-m3:cloud",
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 300.0,
) -> str:
    """Call Ollama-m3 with one photo + the OCR prompt. Return the
    transcript text (not yet written to disk)."""
    user_text = OCR_USER_TEMPLATE.format(
        page=page_number,
        slug=slug,
        page_context_line=_format_page_context(page_context),
    )
    return chat_with_images(
        user_text,
        [photo_path],
        system=OCR_SYSTEM,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )


def ocr_batch(
    photo_paths: list[Path],
    slug: str,
    page_numbers: list[int],
    page_contexts: dict[int, str] | None = None,
    *,
    model: str = "minimax-m3:cloud",
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 300.0,
    repo_root: Path = REPO_ROOT,
) -> list[Path]:
    """Run OCR for N photos one at a time, writing each
    transcript to intake/<slug>/<NN>.transcript.md.

    Existing transcripts at the same path are preserved as
    *.transcript.md.bak, matching the convention in ocr_batch.py.
    """
    if len(photo_paths) != len(page_numbers):
        raise ValueError(
            f"photo_paths ({len(photo_paths)}) and page_numbers "
            f"({len(page_numbers)}) must have the same length."
        )
    intake_dir = repo_root / "intake" / slug
    intake_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for photo, page_num in zip(photo_paths, page_numbers):
        page_context = (page_contexts or {}).get(page_num)
        print(
            f"[ollama-m3] OCR page {page_num:02d}: {photo.name}",
            flush=True,
        )
        text = ocr_photo(
            photo,
            slug,
            page_num,
            page_context=page_context,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
        dest = intake_dir / f"{page_num:02d}.transcript.md"
        if dest.exists():
            bak = dest.with_suffix(".transcript.md.bak")
            print(f"  backing up {dest.name} -> {bak.name}", flush=True)
            dest.replace(bak)
        # Strip wrapping ``` fences if the model emitted them. The
        # downstream parser in publish.py expects the transcript
        # to start with the page-identification block, not with a
        # code fence. (Codex doesn't wrap; minimax-m3 often does.)
        import re
        text = re.sub(r"^\s*```(?:[a-z]+)?\s*\n", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"\n```\s*$", "", text, count=1)
        dest.write_text(text, encoding="utf-8")
        print(f"  wrote {dest} ({len(text)} chars)", flush=True)
        written.append(dest)
    return written
