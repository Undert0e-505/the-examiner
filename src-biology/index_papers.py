"""
src/index_papers.py — Phase 1 of the-examiner pipeline.

Discovers QP and MS PDFs in papers/, pairs them by content, and writes
per-pair metadata + the master index. Text-only — no LLM, no vision.
Designed so the per-question extraction (Phase 2) can be a separate
script that reads what this writes.

Inputs
------
papers/<whatever>.pdf  — original filenames, never renamed. The indexer
                         must work on the content, not the filename.

Outputs
-------
papers/<slug>/meta.json        — one per PDF: kind, board, spec, subject,
                                 paper, tier, exam_date, total_marks,
                                 time_allowed, source_path, pages,
                                 text_chars
papers/<slug>/kvdb-bucket.txt  — stable bucket id (UUIDv5, derived from
                                 pair identity; never changes across
                                 re-runs of the indexer)
papers/<slug>/raw/<basename>.txt   — per-page text dump of the QP,
                                     kept under the slug folder so
                                     Phase 2 (LLM extraction) can read
                                     it without re-PDF-parsing
papers/<slug>/raw/<basename>.ms.txt — same for the MS
index/papers.json              — master list, all pairs, with their
                                 meta + slug + bucket id

Slug
----
<board>-<spec_no_slash>-<subject_slug>-<tier?>-<YYYY>-<MM>

e.g. edexcel-1ma11h-mathematics-higher-2024-11
     aqa-84621h-chemistry-higher-2024-06
     aqa-87021-english-literature-2024-06

Lowercase, ASCII, dashes only. Tier is included only when the paper
is tiered.

KVdb bucket
-----------
UUIDv5 over a stable seed (board|spec|paper|exam_date). Re-running
the indexer produces the same id. This is the contract: the bucket
is assigned once at index time and never changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import uuid
from typing import Any

import pymupdf  # noqa: F401

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PAPERS_DIR = REPO_ROOT / "papers"
INDEX_DIR = REPO_ROOT / "index"

# ---------------------------------------------------------------------------
# Spec → subject map. The cover page does say "Mathematics" / "CHEMISTRY" /
# "ENGLISH LITERATURE" but we cross-check against this so a hand-renamed
# PDF can't fool us. Extend as new specs come in.
# ---------------------------------------------------------------------------
SPEC_SUBJECT: dict[str, str] = {
    "1MA1": "mathematics",
    "8462": "chemistry",
    "8702": "english-literature",
    "8700": "english-language",
    "8463": "physics",
    "8464": "biology",
    "8300": "mathematics",   # AQA maths
    "1BI1": "biology",       # Edexcel biology
    "1CH1": "chemistry",     # Edexcel chemistry
    "1PH1": "physics",       # Edexcel physics
}

# ---------------------------------------------------------------------------
# Regex catalogue
# ---------------------------------------------------------------------------
# A spec code on the cover looks like "1MA1/1H", "8462/1H", "8702/1".
# The first 4 (or 4-digit) part is the spec; the part after the slash is
# the paper reference; if it ends in H or F it's the tier.
RE_SPEC_PAPER = re.compile(
    r"\b(?P<spec>(?:[0-9]{4}|[0-9][A-Z]{2}[0-9]))\s*/\s*(?P<paper>[0-9][A-Z]?)\b"
)

# Edexcel style: "1MA1/1H" appears on cover AND inside text. AQA cover has
# the spec+paper on its own line; the AQA file footer uses
# "8462/1H" / "8702/1". We catch both.

# Edexcel "Pearson Edexcel" → board = edexcel. AQA "AQA" → aqa.
RE_PEARSON = re.compile(r"pearson\s+edexcel|©\s*\d{4}\s*pearson", re.IGNORECASE)
RE_AQA = re.compile(r"\bAQA\b|@aqa\.org\.uk", re.IGNORECASE)
RE_OCR = re.compile(r"\bOCR\b|oxford\s+cambridge\s+and\s+rsa", re.IGNORECASE)
RE_EDUQAS = re.compile(r"\beduqas\b", re.IGNORECASE)
RE_WJEC = re.compile(r"\bwjec\b", re.IGNORECASE)
# Total marks phrases
RE_TOTAL_MARKS = re.compile(
    r"the\s+total\s+mark\s+for\s+this\s+paper\s+is\s+(?P<m>\d+)",
    re.IGNORECASE,
)
RE_MAX_MARK = re.compile(
    r"the\s+maximum\s+mark\s+for\s+this\s+paper\s+is\s+(?P<m>\d+)",
    re.IGNORECASE,
)
# Time allowed
RE_TIME = re.compile(
    r"time\s+allowed:\s*(?P<t>\d+\s*hours?\s*\d*\s*minutes?)",
    re.IGNORECASE,
)
RE_TIME_PAREN = re.compile(
    r"\(\s*time:\s*(?P<t>\d+\s*hours?\s*\d*\s*minutes?)\s*\)",
    re.IGNORECASE,
)
# Tier
RE_TIER = re.compile(
    r"\b(?P<tier>higher|foundation)\s+tier\b", re.IGNORECASE
)
# MS-specific
RE_MARK_SCHEME_HEADER = re.compile(r"^mark\s+scheme\s*\(results\)\s*$", re.IGNORECASE | re.MULTILINE)
RE_MARK_SCHEME_BODY = re.compile(r"\bmark\s+scheme\b", re.IGNORECASE)
RE_VERSION = re.compile(r"version\s*:\s*([\d.]+)\s*final", re.IGNORECASE)
# Exam date
RE_DAY_MONTH_YEAR = re.compile(
    r"\b(?P<day>\d{1,2})\s+(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
RE_MONTH_YEAR = re.compile(
    r"\b(?P<month>january|february|march|april|may|june|july|august|september|october|november|december)\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
RE_WEEKDAY = re.compile(
    r"\b(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
# Subject keywords (cross-checked against SPEC_SUBJECT)
SUBJECT_KEYWORDS: dict[str, list[str]] = {
    "mathematics": ["mathematics", "maths"],
    "chemistry": ["chemistry"],
    "physics": ["physics"],
    "biology": ["biology"],
    "english-language": ["english language"],
    "english-literature": ["english literature"],
}

MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def slugify_subject(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def norm(s: str) -> str:
    """Normalise whitespace for matching."""
    return re.sub(r"\s+", " ", s).strip()


def page_text(doc: pymupdf.Document, n: int) -> str:
    return doc[n].get_text()


def join_pages(doc: pymupdf.Document, n: int | None = None) -> str:
    if n is None:
        return "\n".join(page_text(doc, i) for i in range(doc.page_count))
    return page_text(doc, n)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def detect_board(text: str) -> str | None:
    if RE_PEARSON.search(text):
        return "edexcel"
    if RE_AQA.search(text):
        return "aqa"
    if RE_OCR.search(text):
        return "ocr"
    if RE_EDUQAS.search(text):
        return "eduqas"
    if RE_WJEC.search(text):
        return "wjec"
    return None


def detect_spec_paper(text: str) -> tuple[str, str] | None:
    m = RE_SPEC_PAPER.search(text)
    if not m:
        return None
    return m.group("spec").upper(), m.group("paper").upper()


def detect_kind(text: str) -> str:
    """qp or ms. MS covers have 'Mark scheme' or 'Mark Scheme (Results)' on page 1."""
    head = text[:3000]
    if RE_MARK_SCHEME_HEADER.search(head) or re.search(r"^\s*mark\s+scheme\s*$", head, re.IGNORECASE | re.MULTILINE):
        return "ms"
    if RE_VERSION.search(head):
        return "ms"
    if RE_MARK_SCHEME_BODY.search(head) and not RE_TOTAL_MARKS.search(head) and not RE_MAX_MARK.search(head):
        return "ms"
    return "qp"


def detect_tier(text: str) -> str | None:
    m = RE_TIER.search(text)
    if not m:
        return None
    return m.group("tier").lower()


def detect_total_marks(text: str) -> int | None:
    m = RE_TOTAL_MARKS.search(text) or RE_MAX_MARK.search(text)
    if m:
        return int(m.group("m"))
    return None


def detect_time_allowed(text: str) -> str | None:
    m = RE_TIME.search(text) or RE_TIME_PAREN.search(text)
    if m:
        return norm(m.group("t"))
    return None


def detect_exam_date(text: str) -> tuple[str, str | None] | None:
    """Return (YYYY-MM, raw_phrase) — full date when we have day+month+year,
    month+year otherwise. The MS often only carries month+year, so we
    downgrade gracefully."""
    m = RE_DAY_MONTH_YEAR.search(text)
    if m:
        y = int(m.group("year"))
        mo = MONTH_NUM[m.group("month").lower()]
        return f"{y:04d}-{mo:02d}", m.group(0)
    m = RE_MONTH_YEAR.search(text)
    if m:
        y = int(m.group("year"))
        mo = MONTH_NUM[m.group("month").lower()]
        return f"{y:04d}-{mo:02d}", m.group(0)
    return None


def detect_subject(text: str, spec: str | None) -> str | None:
    """Spec map first, then text keyword match (longest-first)."""
    if spec and spec in SPEC_SUBJECT:
        return SPEC_SUBJECT[spec]
    head = text[:3000].lower()
    for subj in sorted(SUBJECT_KEYWORDS.keys(), key=lambda s: -len(s)):
        for kw in SUBJECT_KEYWORDS[subj]:
            if kw in head:
                return subj
    return None


def build_slug(
    board: str | None,
    spec: str | None,
    paper: str | None,
    subject: str | None,
    tier: str | None,
    exam_ym: str | None,
) -> str:
    parts: list[str] = []
    parts.append((board or "unknown").lower())
    if spec and paper:
        parts.append(f"{spec.lower()}{paper.lower()}")
    elif spec:
        parts.append(spec.lower())
    if subject:
        parts.append(slugify_subject(subject))
    if tier:
        parts.append(tier.lower())
    if exam_ym:
        parts.append(exam_ym)
    return "-".join(parts)


def kvdb_bucket_id(board: str, spec: str, paper: str, exam_ym: str) -> str:
    """UUIDv5 over a stable seed. Idempotent across re-runs."""
    seed = f"{board}|{spec}|{paper}|{exam_ym}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"the-examiner.kvdb.bucket|{seed}"))


# ---------------------------------------------------------------------------
# Per-PDF metadata extraction
# ---------------------------------------------------------------------------
def extract_meta(pdf_path: pathlib.Path) -> dict[str, Any]:
    doc = pymupdf.open(pdf_path)
    try:
        cover = page_text(doc, 0)
        body = join_pages(doc)
        text_for_search = cover + "\n" + body
        board = detect_board(text_for_search)
        spec_paper = detect_spec_paper(text_for_search)
        if spec_paper:
            spec, paper = spec_paper
        else:
            spec, paper = None, None
        kind = detect_kind(cover)
        tier = detect_tier(cover) if kind == "qp" else None
        # Try to pull total marks from cover only; some MS covers have it
        # as well, but we only care for QP.
        total = detect_total_marks(cover) if kind == "qp" else None
        time_allowed = detect_time_allowed(cover) if kind == "qp" else None
        exam = detect_exam_date(cover)
        exam_ym = exam[0] if exam else None
        # If QP didn't yield exam date from cover, peek at body (mark scheme
        # always has month+year so this is mostly relevant for MS).
        if not exam_ym:
            exam = detect_exam_date(body)
            exam_ym = exam[0] if exam else None
        subject = detect_subject(cover, spec)
        # Slug uses just the exam YM (e.g. 2024-11), not the day
        return {
            "kind": kind,
            "board": board,
            "spec": spec,
            "paper": paper,
            "subject": subject,
            "tier": tier,
            "exam_date_raw": exam[1] if exam else None,
            "exam_ym": exam_ym,
            "total_marks": total,
            "time_allowed": time_allowed,
            "source_path": str(pdf_path.relative_to(REPO_ROOT)),
            "source_filename": pdf_path.name,
            "pages": doc.page_count,
            "text_chars": len(body),
            "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
            "indexed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------
def pair_key(meta: dict[str, Any]) -> str | None:
    """Pairing key uses only spec + paper. The exam date is taken from the
    QP (which is the source of truth for the sitting); the MS often carries
    the publication month instead of the exam month, and conflating them
    splits QP/MS for the same sitting into two groups (AQA publishes its
    MS the month after the exam)."""
    if not meta["board"] or not meta["spec"] or not meta["paper"]:
        return None
    return f"{meta['board']}|{meta['spec']}|{meta['paper']}"


def pair_pdfs(metas: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Group by pair_key. Each pair should have a 'qp' and (usually) an 'ms'."""
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for m in metas:
        k = pair_key(m)
        if k is None:
            continue
        pairs.setdefault(k, {})[m["kind"]] = m
    return pairs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def write_raw_text(slug_dir: pathlib.Path, meta: dict[str, Any]) -> None:
    raw = slug_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    base = pathlib.Path(meta["source_filename"]).stem
    suffix = "" if meta["kind"] == "qp" else ".ms"
    out = raw / f"{base}{suffix}.txt"
    pdf_path = REPO_ROOT / meta["source_path"]
    doc = pymupdf.open(pdf_path)
    try:
        with out.open("w", encoding="utf-8") as fh:
            for i in range(doc.page_count):
                fh.write(f"\n----- PAGE {i+1} -----\n")
                fh.write(doc[i].get_text())
    finally:
        doc.close()
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--papers-dir",
        default=str(PAPERS_DIR),
        help="Where to look for PDFs (default: ./papers)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove slug directories and the index file before re-indexing. "
             "Refuses to remove any directory that doesn't match a known slug "
             "shape, so it's safe.",
    )
    args = parser.parse_args()

    papers_dir = pathlib.Path(args.papers_dir)
    if not papers_dir.is_dir():
        print(f"no such dir: {papers_dir}", file=sys.stderr)
        return 2

    if args.clean:
        for child in papers_dir.iterdir():
            if child.is_dir() and re.match(r"^[a-z0-9][a-z0-9-]+-\d{4}-\d{2}$", child.name):
                print(f"clean: removing {child.relative_to(REPO_ROOT)}")
                import shutil
                shutil.rmtree(child)
        idx = INDEX_DIR / "papers.json"
        if idx.exists():
            print(f"clean: removing {idx.relative_to(REPO_ROOT)}")
            idx.unlink()

    pdfs = sorted(p for p in papers_dir.glob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"no PDFs in {papers_dir}", file=sys.stderr)
        return 1
    print(f"discovered {len(pdfs)} PDF(s) in {papers_dir.relative_to(REPO_ROOT)}/")

    metas: list[dict[str, Any]] = []
    for pdf in pdfs:
        try:
            m = extract_meta(pdf)
        except Exception as exc:
            print(f"  ! failed to index {pdf.name}: {exc}", file=sys.stderr)
            continue
        print(
            f"  - {pdf.name}: kind={m['kind']:2}  board={m['board']!s:8}  "
            f"spec={m['spec']!s:6} paper={m['paper']!s:4} tier={m['tier']!s:10}  "
            f"subject={m['subject']!s:20} exam={m['exam_ym']!s:8}  "
            f"total_marks={m['total_marks']!s:4}  time={m['time_allowed']!s:14}  "
            f"pages={m['pages']:3}"
        )
        metas.append(m)

    pairs = pair_pdfs(metas)
    print(f"paired into {len(pairs)} group(s):")
    for k, kids in pairs.items():
        flags = "/".join(sorted(kids.keys()))
        print(f"  - [{flags}] {k}")

    # Resolve the canonical exam_ym per pair: prefer the QP, fall back to MS.
    pair_exam_ym: dict[str, str] = {}
    for k, kids in pairs.items():
        qp = kids.get("qp")
        ms = kids.get("ms")
        # The MS can sometimes be a clearer source of the sitting month for
        # Edexcel (which writes "Mark Scheme (Results) November 2024"), but
        # in general trust the QP. If neither, leave as 'unknown' and the
        # slug will carry it.
        exam_ym = None
        if qp and qp.get("exam_ym"):
            exam_ym = qp["exam_ym"]
        elif ms and ms.get("exam_ym"):
            exam_ym = ms["exam_ym"]
        if exam_ym:
            pair_exam_ym[k] = exam_ym

    # Write per-pair directory + meta.json + kvdb-bucket.txt + raw/
    papers_index: list[dict[str, Any]] = []
    for key, kids in pairs.items():
        # Slug is derived from the QP if we have one; otherwise the MS.
        primary = kids.get("qp") or kids.get("ms")
        assert primary is not None
        board = primary["board"] or "unknown"
        spec = primary["spec"] or "unknown"
        paper = primary["paper"] or "0"
        # Use pair-level exam_ym (QP preferred). If unknown, fall back to
        # whatever the primary file carried, or "unknown-00" so the slug
        # still has a stable shape.
        exam_ym = pair_exam_ym.get(key) or primary.get("exam_ym") or "unknown-00"
        subject = primary["subject"] or "unknown"
        tier = primary["tier"]
        slug = build_slug(board, spec, paper, subject, tier, exam_ym)
        slug_dir = papers_dir / slug
        slug_dir.mkdir(parents=True, exist_ok=True)
        # Write per-kind meta.json
        for kind, m in kids.items():
            out = slug_dir / f"meta.{kind}.json"
            out.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
            write_raw_text(slug_dir, m)
        # Pair-level meta
        bucket = kvdb_bucket_id(board, spec, paper, exam_ym)
        (slug_dir / "kvdb-bucket.txt").write_text(bucket + "\n", encoding="utf-8")
        pair_meta = {
            "slug": slug,
            "board": board,
            "spec": spec,
            "paper": paper,
            "subject": subject,
            "tier": tier,
            "exam_ym": exam_ym,
            "kvdb_bucket": bucket,
            "files": {k: m["source_filename"] for k, m in kids.items()},
        }
        (slug_dir / "pair.json").write_text(json.dumps(pair_meta, indent=2), encoding="utf-8")
        papers_index.append(pair_meta)
        print(f"  wrote {slug_dir.relative_to(REPO_ROOT)}/  (bucket {bucket[:8]}…)")

    # Master index
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    index_path = INDEX_DIR / "papers.json"
    payload = {
        "schema_version": 1,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "paper_count": len(papers_index),
        "papers": papers_index,
    }
    index_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {index_path.relative_to(REPO_ROOT)}  ({len(papers_index)} paper(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
