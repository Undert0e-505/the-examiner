"""
scripts/test_m3_e2e.py — full end-to-end m3 pipeline in the fork.

Stages:
  1. Discover: feed the 26 chemistry photos to minimax-m3 via
     the discover_batch adapter; write DISCOVERY.json.
  2. OCR: run the OCR adapter on every photo, writing
     intake/<slug>/<NN>.transcript.md. Existing transcripts are
     preserved as .bak.
  3. Mark: run the mark adapter on every question, writing
     assessments/<slug>/Q<NN>.marking.md.
  4. Summary: render SUMMARY.md from the Q-NN files using
     publish.parse_summary (the same parser the orchestrator uses).
  5. Publish: render the assessment HTML to
     pages/assessments/<slug>.html (no push, no email).
  6. Report: print the per-question tally + total + comparison
     to the canonical 65/100.

Run from D:\\dev\\the-examiner-m3 with the same Python (3.10.11).
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO = Path(r"D:\dev\the-examiner-m3")
sys.path.insert(0, str(REPO / "src"))

from backends.ollama_m3 import discover_with_ollama_m3  # noqa: E402
from backends.ollama_m3_ocr import ocr_batch  # noqa: E402
from backends.ollama_m3_mark import mark_batch  # noqa: E402
import publish  # noqa: E402

import discover_batch as db  # noqa: E402

# Chemistry paper the canonical repo already has an assessment for.
SLUG = "aqa-84621h-chemistry-higher-2024-05"
JOB = f"m3-e2e-{int(time.time())}"

# Canonical tally from assessments/<slug>/SUMMARY.md (morning run).
CANONICAL_TALLY = {
    1: (8, 10),
    2: (11, 11),
    3: (7, 8),
    4: (11, 14),
    5: (9, 10),
    6: (2, 11),
    7: (8, 15),
    8: (4, 9),
    9: (5, 12),
}
CANONICAL_TOTAL = sum(a for a, _ in CANONICAL_TALLY.values())  # 65


def stage_photos(photos: list[Path]) -> list[Path]:
    """Copy the 26 photos into the fork's intake/_discover/<job>/
    and restage them to intake/<slug>/<page>.jpg using the
    discovered page numbers. Returns the staged paths."""
    intake_dir, copied = db.stage_photos_for_discovery(photos, JOB)
    print(f"[e2e] staged {len(copied)} photos into {intake_dir}", flush=True)
    return copied


def run_discover(copied: list[Path]) -> dict:
    """Call the discover adapter; return the parsed dict."""
    parsed = discover_with_ollama_m3(copied, JOB)
    return parsed


def page_numbers_by_index(discovery: dict) -> dict[int, int]:
    """Convert the string-keyed page_numbers to int keys (cover -> 1)."""
    out: dict[int, int] = {}
    for k, v in discovery.get("page_numbers", {}).items():
        idx = int(k)
        if v is None and idx == 1:
            out[idx] = 1  # cover defaults to page 1
        elif v is not None:
            try:
                out[idx] = int(v)
            except (TypeError, ValueError):
                pass
    return out


def main(argv=None) -> int:
    import argparse
    import shutil
    p = argparse.ArgumentParser()
    p.add_argument(
        "--skip-ocr-and-mark", action="store_true",
        help="Skip the OCR + marking stages (assume the per-Q "
             "transcripts and marking files are already in place). "
             "Used to re-run just the SUMMARY synthesis + tally + "
             "publish after fixing the script.",
    )
    p.add_argument(
        "--run-number", type=int, default=None,
        help="Label for this run (e.g. 2, 3). When set, the driver "
             "auto-snapshots the current transcripts+markings+summary "
             "to snapshots/run_<timestamp>_<N>/ before overwriting, "
             "so multiple runs can be compared side-by-side.",
    )
    args = p.parse_args(argv)

    # 0. Snapshot the current state (so we don't lose the previous run).
    if args.run_number is not None:
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        snap_dir = REPO / "snapshots" / f"run_{ts}_#{args.run_number}"
        snap_dir.mkdir(parents=True, exist_ok=True)
        print(f"[e2e] snapshotting previous run to {snap_dir}", flush=True)
        # Transcripts
        for src in (REPO / "intake" / SLUG).glob("*.transcript.md"):
            dst = snap_dir / "transcripts" / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        # Marking + SUMMARY
        for src in (REPO / "assessments" / SLUG).glob("*.md"):
            dst = snap_dir / src.name
            shutil.copy2(src, dst)
        print(
            f"[e2e] snapshot: "
            f"{len(list((snap_dir / 'transcripts').glob('*.transcript.md')))} transcripts, "
            f"{len(list(snap_dir.glob('Q*.marking.md')))} Q files, "
            f"SUMMARY.md",
            flush=True,
        )

    # 1. Pick the 26 chemistry photos from the gateway cache
    #    (oldest-first, by LastWriteTime). We use the same selector
    #    the orchestrator would.
    cache = Path(r"C:\Users\openclaw-agent\.openclaw\media\inbound")
    photos = sorted(cache.glob("*.jpg"), key=lambda p: p.stat().st_mtime)[-26:]
    print(f"[e2e] using {len(photos)} photos from cache", flush=True)

    if not args.skip_ocr_and_mark:
        # 2. Stage
        copied = stage_photos(photos)

        # 3. Discover
        print(f"\n=== STAGE 1: DISCOVER (m3) ===\n", flush=True)
        t0 = time.time()
        discovery = run_discover(copied)
        print(f"[e2e] discover done in {time.time() - t0:.1f}s", flush=True)
        print(
            f"[e2e] cover_paper_code={discovery.get('cover_paper_code')!r}, "
            f"slug={SLUG}, page_numbers={discovery.get('page_numbers')}",
            flush=True,
        )

        # 4. Map file index -> printed page
        page_nums = page_numbers_by_index(discovery)
        # Build the parallel photo list in receipt order (file index 1..N).
        ordered_photos: list[Path] = []
        ordered_pages: list[int] = []
        for idx in sorted(page_nums.keys()):
            src = REPO / "intake" / "_discover" / JOB / f"{idx:02d}.jpg"
            if src.is_file():
                ordered_photos.append(src)
                ordered_pages.append(page_nums[idx])
        print(
            f"[e2e] OCR list: {len(ordered_photos)} photos, "
            f"pages {ordered_pages}",
            flush=True,
        )

        # 5. OCR
        print(f"\n=== STAGE 2: OCR (m3, {len(ordered_photos)} photos) ===\n", flush=True)
        t0 = time.time()
        transcripts = ocr_batch(
            photo_paths=ordered_photos,
            slug=SLUG,
            page_numbers=ordered_pages,
            repo_root=REPO,
        )
        print(f"[e2e] OCR done in {time.time() - t0:.1f}s, {len(transcripts)} transcripts", flush=True)

        # 6. Mark
        print(f"\n=== STAGE 3: MARK (m3, 9 questions) ===\n", flush=True)
        t0 = time.time()
        markings = mark_batch(slug=SLUG, repo_root=REPO)
        print(f"[e2e] mark done in {time.time() - t0:.1f}s, {len(markings)} Q files", flush=True)
    else:
        print("[e2e] --skip-ocr-and-mark: using existing transcripts and marking files", flush=True)

    # 7. Synthesize SUMMARY.md using the existing parser. The
    # m3 mark adapter writes per-Q files but not a SUMMARY.md;
    # we synthesize one here from the per-Q totals so the publish
    # step can render the page. This is not a "test of m3" -- it's
    # a stitching step in the orchestrator. The m3 mark results
    # themselves are above.
    print(f"\n=== STAGE 4: SUMMARY.md (synthesized) ===\n", flush=True)
    try:
        # Build a minimal SUMMARY.md that parse_summary() can read.
        # We only need: paper_code, sitting, totals, per-Q tally
        # table. Observations and assessor notes are optional.
        q_rows_text = []
        for qn in range(1, 10):
            qp = REPO / "assessments" / SLUG / f"Q{qn:02d}.marking.md"
            if not qp.is_file():
                q_rows_text.append(f"| Q{qn} | ? | ? | (no marking file) |")
                continue
            t = qp.read_text(encoding="utf-8")
            m_a = re.search(
                r"Total marks awarded for this question:\s*\*?\*?\s*(\d+)\s*out of\s*(\d+)",
                t,
            )
            if m_a:
                awarded, available = int(m_a.group(1)), int(m_a.group(2))
            else:
                awarded, available = 0, 0
            q_rows_text.append(f"| Q{qn} | {available} | {awarded} | (see Q{qn:02d}.marking.md for per-criterion breakdown) |")
        # Sum totals from the same regex sweep
        total_awarded = 0
        total_available = 0
        for qn in range(1, 10):
            qp = REPO / "assessments" / SLUG / f"Q{qn:02d}.marking.md"
            if not qp.is_file():
                continue
            t = qp.read_text(encoding="utf-8")
            m_a = re.search(
                r"Total marks awarded for this question:\s*\*?\*?\s*(\d+)\s*out of\s*(\d+)",
                t,
            )
            if m_a:
                total_awarded += int(m_a.group(1))
                total_available += int(m_a.group(2))
        summary_md = (
            "#### 1. Paper header\n\n"
            "- Paper code: 8462/1H\n"
            "- Sitting: May/June 2024, Paper 1H\n"
            "- Total marks available: 100\n"
            f"- Total marks awarded: {total_awarded}\n\n"
            "#### 2. Per-question tally table\n\n"
            "| Question | Marks available | Marks awarded | Notes |\n"
            "|---|---:|---:|---|\n"
            + "\n".join(q_rows_text)
            + "\n\n"
            "#### 3. Cross-paper observations\n\n"
            "(Auto-generated by the m3 end-to-end test driver; for "
            "narrative observations, see the canonical Codex-generated "
            "SUMMARY.md in D:\\dev\\the-examiner\\assessments\\...\\SUMMARY.md.)\n\n"
            "#### 4. Pipeline verdict\n\n"
            "Driver: minimax-m3:cloud via src/backends/ollama_m3*.py. "
            "No Codex involved. Confidence per question depends on the "
            "per-Q marking file's reading note; overall confidence is "
            "high on short text answers and medium on calculation-heavy "
            "questions where m3 has to OCR ambiguous arithmetic.\n"
        )
        summary_path = REPO / "assessments" / SLUG / "SUMMARY.md"
        if summary_path.exists():
            bak = summary_path.with_suffix(".md.bak")
            summary_path.replace(bak)
            print(f"[e2e] backed up existing SUMMARY.md -> {bak.name}", flush=True)
        summary_path.write_text(summary_md, encoding="utf-8")
        print(
            f"[e2e] wrote synthesized {summary_path}: "
            f"{total_awarded}/{total_available}",
            flush=True,
        )
    except Exception as e:
        import traceback
        print(f"[e2e] SUMMARY.md synthesis failed: {e}", flush=True)
        traceback.print_exc()

    # 8. Tally vs canonical
    print(f"\n=== STAGE 5: TALLY ===\n", flush=True)
    m3_tally: dict[int, tuple[int, int]] = {}
    total_awarded = 0
    total_available = 0
    for qn in range(1, 10):
        p = REPO / "assessments" / SLUG / f"Q{qn:02d}.marking.md"
        if not p.is_file():
            m3_tally[qn] = (0, 0)
            continue
        text = p.read_text(encoding="utf-8")
        m_awarded = re.search(
            r"Total marks awarded for this question:\s*\*?\*?\s*(\d+)\s*out of\s*(\d+)",
            text,
        )
        if m_awarded:
            awarded = int(m_awarded.group(1))
            available = int(m_awarded.group(2))
            m3_tally[qn] = (awarded, available)
            total_awarded += awarded
            total_available += available
        else:
            m3_tally[qn] = (0, 0)
    print(f"  Q  m3      canonical  delta")
    for qn in range(1, 10):
        m3a, m3b = m3_tally.get(qn, (0, 0))
        ca, cb = CANONICAL_TALLY.get(qn, (0, 0))
        delta = m3a - ca
        sign = "+" if delta > 0 else ""
        print(f"  Q{qn} {m3a:>2}/{m3b:<2}   {ca:>2}/{cb:<2}     {sign}{delta}")
    print(f"  -- -------- ---------  ----")
    print(f"  TOT {total_awarded:>3}/{total_available:<3}  {CANONICAL_TOTAL:>3}/100      {total_awarded - CANONICAL_TOTAL:+d}")
    pct_m3 = round(100 * total_awarded / total_available) if total_available else 0
    print(f"  m3: {pct_m3}%   canonical: 65%")

    # 9. Publish to local pages/ (no push, no email)
    print(f"\n=== STAGE 6: PUBLISH (local, no push) ===\n", flush=True)
    try:
        student = publish.read_student_json(require_recipient=False)
        meta = publish.publish_one(SLUG, student, dry_run=False)
        publish.publish_index([meta], dry_run=False)
        publish.copy_assets(dry_run=False)
        print(
            f"[e2e] published: "
            f"{meta.get('total_awarded')}/{meta.get('total_available')}, "
            f"bucket={meta.get('kvdb_bucket')}",
            flush=True,
        )
    except Exception as e:
        import traceback
        print(f"[e2e] publish failed: {e}", flush=True)
        traceback.print_exc()

    return 0


if __name__ == "__main__":
    sys.exit(main())
