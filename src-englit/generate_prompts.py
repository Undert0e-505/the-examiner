"""
src/generate_prompts.py - render the per-run Codex prompts from
                           versioned Jinja templates + paper metadata.

This module is the bridge between the auto-pipeline (the top-level
orchestrator in src/run.py) and the codex_lane wrapper. The wrapper
expects a 04_CODEX_PROMPT.md file at a specific path inside the
codex-sandboxes _specs tree. Before, that prompt was hand-written for
each run (per Aaron's request on 2026-06-15, the per-run hand-authored
prompt goes away). Now it is generated from the templates in
src/prompts/ocr.md.j2 and src/prompts/mark.md.j2, with the per-paper
metadata filled in by this module.

Two public functions:
  - render_ocr_prompt(slug, page_numbers, page_contexts, batch_id) -> str
  - render_mark_prompt(slug) -> str

A small CLI is also exposed so the operator can preview the prompt
that WOULD be used for a given paper right now, without doing any
Codex work:

    D:\\Python310\\python.exe src/generate_prompts.py --slug <slug> --stage ocr --page-order 1 2 3 --print
    D:\\Python310\\python.exe src/generate_prompts.py --slug <slug> --stage mark --print

The 'print' mode is the right way to audit what the orchestrator is
about to ask Codex. Review once, trust forever (or until the
template changes).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO_ROOT = Path("D:/dev/the-examiner")
PROMPTS_DIR = REPO_ROOT / "src" / "prompts"
TEMPLATES = {
    "ocr": PROMPTS_DIR / "ocr.md.j2",
    "mark": PROMPTS_DIR / "mark.md.j2",
    "discover": PROMPTS_DIR / "discover.md.j2",
}


def _env() -> Environment:
    """Jinja environment. StrictUndefined so we get a loud error if a
    template variable isn't filled in (better than silently producing
    a half-rendered prompt that would confuse Codex). We use the
    default trim_blocks=False and lstrip_blocks=False, and the
    templates use explicit {%- / -%} whitespace control where they
    need to control blank lines around for-loops and if-blocks.
    This is more verbose than the alternative of trim_blocks=True
    but it's predictable: the template author controls the
    whitespace, not the environment."""
    return Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
        undefined=StrictUndefined,
    )


def _paper_meta(slug: str) -> dict:
    """Read the per-paper metadata files (markscheme.json, paper.json
    if present) and produce the dict the templates need. Raises
    FileNotFoundError with a clear message if the markscheme is missing.
    """
    paper_dir = REPO_ROOT / "papers" / slug
    if not paper_dir.is_dir():
        raise FileNotFoundError(
            f"papers/{slug}/ does not exist. The paper has not been indexed. "
            f"Run `python src/index_papers.py` and `python src/extract_questions.py "
            f"--slug {slug}` to set it up."
        )
    markscheme_path = paper_dir / "markscheme.json"
    if not markscheme_path.is_file():
        raise FileNotFoundError(
            f"papers/{slug}/markscheme.json does not exist. The markscheme "
            f"extractor must produce this file before the marking pass can "
            f"run. Run `python src/extract_questions.py --slug {slug}` first."
        )
    markscheme = json.loads(markscheme_path.read_text(encoding="utf-8"))
    return {
        "slug": slug,
        "markscheme_path": f"papers/{slug}/markscheme.json",
        "markscheme": markscheme,
    }


def _summarise_markscheme(markscheme: dict) -> dict:
    """Pull the per-question totals, total marks, criteria count, and
    a per-question tally string out of the markscheme JSON. These
    are the values the mark template needs.
    """
    marks = markscheme.get("marks", [])
    question_count = len(marks)
    criteria_count = sum(len(m.get("criteria", [])) for m in marks)
    total_marks = sum(m.get("total_marks_for_question", 0) for m in marks)
    per_q = ", ".join(
        f"Q{m.get('paper_question_number', str(i+1))}={m.get('total_marks_for_question', 0)}"
        for i, m in enumerate(marks)
    )
    question_numbers = [
        int(m.get("paper_question_number", str(i + 1))) for i, m in enumerate(marks)
    ]
    return {
        "question_count": question_count,
        "criteria_count": criteria_count,
        "total_marks": total_marks,
        "per_question": per_q,
        "question_numbers": question_numbers,
    }


def _summarise_ocr_pages(slug: str, page_numbers: list[int], page_contexts: dict[int, str] | None) -> list[dict]:
    """Build the per-page list the OCR template needs.

    page_numbers is a list of integers (the printed page numbers
    Aaron told us, in glob order). page_contexts is an optional
    dict {printed_page: "short description like 'Q01.1-1.3, salt
    prep'"} — Aaron can pass this in /mark; if absent we just leave
    context blank and the template prints a bare list of paths.
    """
    if page_contexts is None:
        page_contexts = {}
    return [
        {"number": n, "context": page_contexts.get(n, "")}
        for n in page_numbers
    ]


def _transcripts_list(slug: str) -> tuple[list[int], list[int]]:
    """Discover the OCR transcripts in intake/<slug>/. Returns
    (page_numbers_present, page_numbers_missing) where missing is
    the gaps (e.g. [13, 16, 18] for the chemistry paper, where
    those pages have no answer spaces).
    """
    intake = REPO_ROOT / "intake" / slug
    if not intake.is_dir():
        return ([], [])
    present = []
    for f in sorted(intake.glob("*.transcript.md")):
        m = re.match(r"(\d+)\.transcript\.md$", f.name)
        if m:
            present.append(int(m.group(1)))
    if not present:
        return ([], [])
    full_range = set(range(min(present), max(present) + 1))
    missing = sorted(full_range - set(present))
    return (present, missing)


def render_ocr_prompt(
    slug: str,
    page_numbers: list[int],
    page_contexts: dict[int, str] | None = None,
    batch_id: str | None = None,
    sandbox_dir: str | None = None,
    real_repo: str = "D:/dev/the-examiner",
) -> str:
    """Render the OCR prompt. Returns the rendered markdown as a string.

    page_numbers: list of printed page numbers (e.g. [1, 2, 3, ...]).
                  These determine which transcripts the prompt asks
                  Codex to produce. The prompt template's hard rule
                  "Do not OCR any other photo" makes this an explicit
                  batch boundary.

    page_contexts: optional dict {printed_page: "short description"}.
                   If absent, the prompt just lists paths with no
                   descriptions. Aaron usually supplies this; if he
                   doesn't we get bare paths.

    batch_id: optional label like "A" / "B" / "C" for split batches,
              or None for a single batch. The template uses it to
              emit the right framing sentence.

    sandbox_dir: path to the codex sandbox this prompt will run in.
                 Defaults to the codex-sandboxes sandbox-root with
                 a generated job name.
    """
    if not page_numbers:
        raise ValueError("page_numbers must be non-empty for an OCR prompt")
    job_name = f"ocr-{slug}-{'-'.join(str(n) for n in page_numbers)}"
    if sandbox_dir is None:
        sandbox_dir = f"D:/dev/codex-sandboxes/{job_name}"
    pages = _summarise_ocr_pages(slug, page_numbers, page_contexts)
    env = _env()
    tmpl = env.get_template("ocr.md.j2")
    return tmpl.render(
        sandbox_dir=sandbox_dir,
        real_repo=real_repo,
        slug=slug,
        batch_id=batch_id,
        pages=pages,
    )


def render_mark_prompt(
    slug: str,
    sandbox_dir: str | None = None,
    real_repo: str = "D:/dev/the-examiner",
) -> str:
    """Render the marking prompt. Returns the rendered markdown as a string.

    Reads the markscheme.json and the intake/<slug>/ transcripts to
    figure out what's available. Raises FileNotFoundError if either
    is missing (so the orchestrator can abort with a clear email).
    """
    meta = _paper_meta(slug)
    summary = _summarise_markscheme(meta["markscheme"])
    transcripts, gaps = _transcripts_list(slug)
    if not transcripts:
        raise FileNotFoundError(
            f"intake/{slug}/ has no *.transcript.md files. The OCR pass must "
            f"complete first before the marking pass can run."
        )
    job_name = f"mark-{slug}"
    if sandbox_dir is None:
        sandbox_dir = f"D:/dev/codex-sandboxes/{job_name}"
    summary_text = (
        f"{meta['markscheme'].get('slug', slug)} "
        f"({meta['markscheme'].get('kind', 'paper')})"
    )
    env = _env()
    tmpl = env.get_template("mark.md.j2")
    return tmpl.render(
        sandbox_dir=sandbox_dir,
        real_repo=real_repo,
        slug=slug,
        markscheme_path=meta["markscheme_path"],
        markscheme_summary=summary_text,
        total_marks=summary["total_marks"],
        transcripts=transcripts,
        gaps=gaps,
        question_count=summary["question_count"],
        criteria_count=summary["criteria_count"],
        per_question=summary["per_question"],
        question_numbers=summary["question_numbers"],
    )


def render_discover_prompt(
    photos: list[dict],
    sandbox_dir: str | None = None,
    real_repo: str = "D:/dev/the-examiner",
) -> str:
    """Render the discovery prompt. Returns the rendered markdown as
    a string.

    The discovery prompt asks Codex to:
      - read the cover page and extract the paper code
      - read the printed page number on each photo
      - read the question numbers on each photo (best-effort)
    It does NOT OCR the student's handwritten answers - that is a
    separate pass.

    photos: list of {"path": str, "index": int} dicts. The path
            is the relative path inside the Codex sandbox (the
            wrapper copies the staged photos into the sandbox at
            the same relative location). The index is the
            1-based file index for the prompt's references.
    """
    if not photos:
        raise ValueError("photos must be non-empty for a discovery prompt")
    job_name = "discover-batch"
    if sandbox_dir is None:
        sandbox_dir = f"D:/dev/codex-sandboxes/{job_name}"
    env = _env()
    tmpl = env.get_template("discover.md.j2")
    return tmpl.render(
        sandbox_dir=sandbox_dir,
        real_repo=real_repo,
        photos=photos,
    )


def write_prompt_to_spec_path(content: str, slug: str, stage: str) -> Path:
    """Write the rendered prompt to the path the codex_lane wrapper
    expects: D:/dev/codex-sandboxes/_specs/<jobname>/04_CODEX_PROMPT.md.

    The job name is derived from the stage + slug. The directory is
    created if it doesn't exist. The wrapper reads 04_CODEX_PROMPT.md
    from inside the spec dir, and codex_lane will (in a future
    version) auto-detect this.
    """
    if stage == "ocr":
        job_name = f"ocr-{slug}"
    elif stage == "mark":
        job_name = f"mark-{slug}"
    elif stage == "discover":
        job_name = "discover-batch"
    else:
        raise ValueError(f"unknown stage: {stage!r}")
    spec_dir = Path("D:/dev/codex-sandboxes/_specs") / job_name
    spec_dir.mkdir(parents=True, exist_ok=True)
    out = spec_dir / "04_CODEX_PROMPT.md"
    out.write_text(content, encoding="utf-8")
    return out


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Render the per-run Codex prompt from a Jinja template + paper metadata. "
            "Use --print to preview the prompt without writing it; use --write to "
            "drop it at the path the codex_lane wrapper expects."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--slug", required=True, help="Paper slug (e.g. 'aqa-84621h-chemistry-higher-2024-05'). For --stage discover, use '_' or 'unknown' — the slug is being discovered.")
    p.add_argument("--stage", choices=("ocr", "mark", "discover"), required=True, help="Which prompt to render.")
    p.add_argument(
        "--page-order",
        type=int,
        nargs="*",
        default=None,
        help="OCR only: printed page numbers in the order Aaron specified. "
             "If absent, the OCR prompt is for all transcripts in intake/<slug>/.",
    )
    p.add_argument(
        "--page-contexts",
        default=None,
        help="OCR only: JSON dict {page_num: 'short context'}, e.g. '{\"1\":\"cover\", \"2\":\"Q01.1-1.3\"}'.",
    )
    p.add_argument("--batch-id", default=None, help="OCR only: batch label like 'A' / 'B' / 'C'.")
    p.add_argument(
        "--print",
        action="store_true",
        dest="do_print",
        help="Print the rendered prompt to stdout. (Default behaviour; here for explicitness.)",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="Write the rendered prompt to the codex_lane spec path (D:/dev/codex-sandboxes/_specs/<job>/04_CODEX_PROMPT.md).",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.stage == "ocr":
        page_order = args.page_order
        if page_order is None:
            # Default to all transcripts currently in intake/<slug>/
            present, _ = _transcripts_list(args.slug)
            page_order = present
        page_contexts = None
        if args.page_contexts:
            page_contexts = json.loads(args.page_contexts)
        content = render_ocr_prompt(
            args.slug,
            page_order,
            page_contexts=page_contexts,
            batch_id=args.batch_id,
        )
    elif args.stage == "mark":
        content = render_mark_prompt(args.slug)
    else:  # discover
        if not args.page_order:
            print("ERROR: --page-order (a list of photo paths) is required for --stage discover.", file=sys.stderr)
            return 1
        # Build the photos list with relative paths (the wrapper
        # copies staged photos into the sandbox at intake/_discover/...).
        photos_for_prompt = [
            {"path": f"intake/_discover/discover-batch/{i+1:02d}.jpg",
             "index": i + 1}
            for i in range(len(args.page_order))
        ]
        content = render_discover_prompt(photos=photos_for_prompt)

    if args.write:
        out = write_prompt_to_spec_path(content, args.slug, args.stage)
        print(f"Wrote {len(content)} chars to {out}", file=sys.stderr)
    if args.do_print or not args.write:
        # Print the full rendered prompt to stdout (pipe-able to a file or pager).
        # Force UTF-8 on the stdout stream so the Windows cp1252 default doesn't
        # mangle Unicode (the templates use −, ±, etc.).
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, io.UnsupportedOperation):
            pass
        sys.stdout.write(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
