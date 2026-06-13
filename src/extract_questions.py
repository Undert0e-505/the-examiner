"""
src/extract_questions.py — Phase 2 of the-examiner pipeline.

Reads the per-pair `raw/<basename>.txt` files written by index_papers.py
and uses the LLM (default: `minimax-m3:cloud` via Ollama) to extract
structured per-question and per-mark data. Writes:

  papers/<slug>/paper.json       — one entry per question / sub-part
  papers/<slug>/markscheme.json  — one entry per question, criteria[] per AO/step

Idempotent on the slug side: re-running overwrites the JSONs. The slug
directory and `kvdb-bucket.txt` are owned by index_papers.py and are
not touched here.

Usage:
    python src/extract_questions.py             # all pairs in papers/
    python src/extract_questions.py aqa-87021-english-literature-2024-05
    python src/extract_questions.py --model minimax-m3:cloud --base-url http://127.0.0.1:11434
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from typing import Any

from llm import chat_json, LLMError

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PAPERS_DIR = REPO_ROOT / "papers"
PROMPTS_DIR = pathlib.Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def read_raw(slug_dir: pathlib.Path, basename: str, kind: str) -> str:
    """Return the raw text dump for a QP or MS. `kind` is 'qp' or 'ms'."""
    suffix = "" if kind == "qp" else ".ms"
    path = slug_dir / "raw" / f"{basename}{suffix}.txt"
    if not path.exists():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
PAPER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema_version", "questions"],
    "properties": {
        "schema_version": {"type": "integer"},
        "total_marks": {"type": ["integer", "null"]},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "paper_question_number", "marks_available", "page_start", "page_end"],
                "properties": {
                    "id": {"type": "string"},
                    "paper_question_number": {"type": "string"},
                    "section": {"type": ["string", "null"]},
                    "extract": {"type": ["string", "null"]},
                    "prompt": {"type": ["string", "null"]},
                    "marks_available": {"type": ["integer", "null"]},
                    "page_start": {"type": "integer"},
                    "page_end": {"type": "integer"},
                },
            },
        },
    },
}

MARKSCHEME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema_version", "marks"],
    "properties": {
        "schema_version": {"type": "integer"},
        "marks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question_ref", "paper_question_number", "total_marks_for_question", "criteria", "page_start", "page_end"],
                "properties": {
                    "question_ref": {"type": "string"},
                    "paper_question_number": {"type": "string"},
                    "total_marks_for_question": {"type": ["integer", "null"]},
                    "criteria": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["marks", "indicative_content"],
                            "properties": {
                                "ao": {"type": ["string", "null"]},
                                "marks": {"type": "integer"},
                                "indicative_content": {"type": "array", "items": {"type": "string"}},
                                "spec_refs": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                    "page_start": {"type": "integer"},
                    "page_end": {"type": "integer"},
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Per-pair extraction
# ---------------------------------------------------------------------------
def find_pairs() -> list[pathlib.Path]:
    """Return slug dirs that have both a QP and an MS pair.json entry."""
    out: list[pathlib.Path] = []
    for child in sorted(PAPERS_DIR.iterdir()):
        if not child.is_dir():
            continue
        pair_path = child / "pair.json"
        if not pair_path.exists():
            continue
        try:
            pair = json.loads(pair_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        files = pair.get("files") or {}
        if "qp" in files and "ms" in files:
            out.append(child)
    return out


def extract_paper(slug_dir: pathlib.Path, model: str, base_url: str, *, timeout: float = 600.0, max_retries: int = 2) -> dict[str, Any]:
    pair = json.loads((slug_dir / "pair.json").read_text(encoding="utf-8"))
    qp_basename = pathlib.Path(pair["files"]["qp"]).stem
    body = read_raw(slug_dir, qp_basename, "qp")
    template = load_prompt("extract_qp.txt")
    prelude, _, tail = template.partition("PAPER TEXT:")
    user_payload = tail.replace("{body}", body)
    return chat_json(
        user_payload,
        system=prelude.rstrip(),
        schema=PAPER_SCHEMA,
        model=model,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def extract_markscheme(slug_dir: pathlib.Path, model: str, base_url: str, *, timeout: float = 600.0, max_retries: int = 2) -> dict[str, Any]:
    pair = json.loads((slug_dir / "pair.json").read_text(encoding="utf-8"))
    ms_basename = pathlib.Path(pair["files"]["ms"]).stem
    body = read_raw(slug_dir, ms_basename, "ms")
    template = load_prompt("extract_ms.txt")
    prelude, _, tail = template.partition("MARK SCHEME TEXT:")
    user_payload = tail.replace("{body}", body)
    return chat_json(
        user_payload,
        system=prelude.rstrip(),
        schema=MARKSCHEME_SCHEMA,
        model=model,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "slugs",
        nargs="*",
        help="Optional list of slug dirs to process. Default: all in papers/.",
    )
    parser.add_argument("--model", default="minimax-m3:cloud")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--timeout", type=float, default=600.0,
        help="Per-call LLM timeout in seconds (default 600).",
    )
    parser.add_argument(
        "--max-retries", type=int, default=2,
        help="Retries on 5xx/429/connection (default 2).",
    )
    parser.add_argument(
        "--kind",
        choices=["both", "qp", "ms"],
        default="both",
        help="Which side to extract (default: both).",
    )
    args = parser.parse_args()

    if args.slugs:
        slug_dirs = [PAPERS_DIR / s for s in args.slugs]
        for d in slug_dirs:
            if not d.is_dir():
                print(f"no such slug: {d}", file=sys.stderr)
                return 2
    else:
        slug_dirs = find_pairs()
    if not slug_dirs:
        print("no pairs to process", file=sys.stderr)
        return 1

    print(f"extracting for {len(slug_dirs)} pair(s) using {args.model} @ {args.base_url}")
    for slug_dir in slug_dirs:
        slug = slug_dir.name
        print(f"\n=== {slug} ===")
        if args.kind in ("both", "qp"):
            t0 = time.time()
            try:
                paper = extract_paper(slug_dir, args.model, args.base_url, timeout=args.timeout, max_retries=args.max_retries)
            except LLMError as exc:
                print(f"  ! QP extraction failed: {exc}", file=sys.stderr)
                return 3
            paper["slug"] = slug
            paper["kind"] = "qp"
            paper["extracted_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            paper["extractor"] = {"model": args.model, "base_url": args.base_url}
            (slug_dir / "paper.json").write_text(
                json.dumps(paper, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            n_q = len(paper.get("questions") or [])
            tm = paper.get("total_marks")
            print(f"  paper.json  {n_q:3} question(s)  total_marks={tm}  ({time.time()-t0:.1f}s)")
        if args.kind in ("both", "ms"):
            t0 = time.time()
            try:
                ms = extract_markscheme(slug_dir, args.model, args.base_url, timeout=args.timeout, max_retries=args.max_retries)
            except LLMError as exc:
                print(f"  ! MS extraction failed: {exc}", file=sys.stderr)
                return 3
            ms["slug"] = slug
            ms["kind"] = "ms"
            ms["extracted_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            ms["extractor"] = {"model": args.model, "base_url": args.base_url}
            (slug_dir / "markscheme.json").write_text(
                json.dumps(ms, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            n_m = len(ms.get("marks") or [])
            print(f"  markscheme.json  {n_m:3} question(s)  ({time.time()-t0:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
