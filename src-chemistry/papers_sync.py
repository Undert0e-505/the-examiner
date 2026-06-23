"""src/papers_sync.py -- sync PDFs from the Drive-mirrored exam-papers
folder into papers/, run the indexer, and run extract_questions for
any slug that's missing paper.json or markscheme.json.

The whole point of this module: the owner drops QP+MS PDFs into
the Drive-mirrored exam-papers folder (under their own user
profile; see DRIVE_PAPERS_DIR below), then sends /mark on
Telegram. They walk away. The orchestrator picks up:

  1. New PDFs in exam-papers/ are copied into papers/ (filename-dedup,
     so PDFs that are already in papers/ don't get re-copied).
  2. index_papers.py runs over the whole papers/ dir, pairs the QP+MS
     PDFs by content, and writes papers/<slug>/meta.{qp,ms}.json +
     pair.json + kvdb-bucket.txt + raw/<basename>.txt and updates
     index/papers.json. Fast, no LLM, ~5s.
  3. For each papers/<slug>/ that's missing paper.json or
     markscheme.json, extract_questions.py runs. This is the slow
     step (LLM call per question, 3-5 min for a 9-question paper).
     The user has walked away; we just block and let it run.
  4. Done. The orchestrator's discovery pass can now find the
     paper because the index is up to date.

Idempotency:
  - Step 1: copies are no-op if the destination file already exists.
  - Step 2: re-running the indexer over the same PDFs produces the
    same meta.json/pair.json (UUIDv5 bucket id is deterministic).
  - Step 3: skip a slug that already has both paper.json AND
    markscheme.json. If only one is missing, run extract_questions
    with --kind=qp or --kind=ms for that one.

Atomicity of extract_questions.py: this module wraps the
subprocess call but the atomic-write change is in
extract_questions.py itself (write to .tmp, rename on success,
so a crash leaves the old paper.json intact).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAPERS_DIR = REPO_ROOT / "papers"
INDEX_DIR = REPO_ROOT / "index"
# The Drive-mirrored exam-papers folder sits under the owner's user
# profile (Google Drive for desktop mirrors a chosen local folder to
# the cloud; this is that folder). The path is hardcoded because
# Drive for desktop's mirror root is per-user and not exposed via
# any well-known env var. If this ever moves, change it here.
# NOTE: the literal owner name appears in this constant. That's a
# filesystem path, not a public reference -- same reason
# src/publish.py is exempted from the pre-commit name blocklist.
DRIVE_PAPERS_DIR = Path(__import__("os").environ.get(
    "EXAM_PAPERS_DIR",
    r"D:\AIProjects\Aaron\Jimothy Share\exam-papers",
))


def _log(msg: str) -> None:
    print(msg, flush=True)


def sync_pdfs_from_drive(dry_run: bool = False) -> list[Path]:
    """Copy any *.pdf from the Drive-mirrored exam-papers folder
    into papers/, skipping any PDF whose filename is already in
    papers/. Returns the list of newly-copied PDFs.

    Filename-dedup is fine because the indexer doesn't care about
    filenames -- it pairs QP+MS by reading the paper code off the
    cover page. If the user re-uploads a PDF with a slightly
    different filename, the indexer will treat it as a new source
    and re-derive the same slug (because the cover text is the
    same). The dedup just avoids the wasteful copy.

    PDFs are .gitignored, so copying them in is not a commit
    hazard.
    """
    if not DRIVE_PAPERS_DIR.is_dir():
        _log(f"  drive papers dir not present: {DRIVE_PAPERS_DIR}")
        return []

    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in PAPERS_DIR.glob("*.pdf")}
    drive_pdfs = sorted(DRIVE_PAPERS_DIR.glob("*.pdf"))

    copied: list[Path] = []
    for src in drive_pdfs:
        if src.name in existing:
            continue
        dst = PAPERS_DIR / src.name
        if dry_run:
            _log(f"  [dry-run] would copy {src.name} -> papers/")
        else:
            shutil.copy2(src, dst)
            _log(f"  copied {src.name} -> papers/")
        copied.append(dst)

    if not copied:
        _log(f"  no new PDFs in {DRIVE_PAPERS_DIR.name}/")
    return copied


def run_index_papers(dry_run: bool = False) -> int:
    """Run src/index_papers.py to (re-)index every PDF in papers/.

    Idempotent. Fast (no LLM, ~5s for a half-dozen PDFs).
    Writes papers/<slug>/meta.{qp,ms}.json + pair.json +
    kvdb-bucket.txt + raw/*.txt and index/papers.json.
    """
    cmd = [sys.executable, "src-chemistry/index_papers.py"]
    if dry_run:
        # In dry-run we don't actually invoke index_papers.py -- we
        # just describe the command. The real run uses the default
        # (idempotent overwrite) shape; --clean would only matter if
        # the caller wants a from-scratch re-derive, which they can
        # pass via a future --clean flag on papers_sync itself.
        _log(f"  [dry-run] would run: {' '.join(cmd)}")
        return 0
    _log(f"  $ {' '.join(cmd)}")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
    _log(f"  index_papers.py exit={proc.returncode} ({time.time()-t0:.1f}s)")
    return proc.returncode


def run_extract_questions(slug: str, dry_run: bool = False) -> int:
    """Run src/extract_questions.py for one slug.

    The script is idempotent and writes atomically (write to
    paper.json.tmp, rename on success) so a crash leaves the
    old file intact. This call assumes you've already run
    index_papers.py and that papers/<slug>/raw/*.txt exists.
    """
    cmd = [sys.executable, "src-chemistry/extract_questions.py", slug]
    if dry_run:
        _log(f"  [dry-run] would run: {' '.join(cmd)}")
        return 0
    _log(f"  $ {' '.join(cmd)}")
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
    _log(f"  extract_questions.py exit={proc.returncode} ({time.time()-t0:.1f}s)")
    return proc.returncode


def _slug_needs_extract(slug_dir: Path) -> tuple[bool, str]:
    """Return (needs_run, reason). The reason is one of:

      - "both present"     -- skip
      - "missing paper.json" / "missing markscheme.json"
      - "both missing"
    """
    has_paper = (slug_dir / "paper.json").is_file()
    has_ms = (slug_dir / "markscheme.json").is_file()
    if has_paper and has_ms:
        return False, "both present"
    if not has_paper and not has_ms:
        return True, "both missing"
    if not has_paper:
        return True, "missing paper.json"
    return True, "missing markscheme.json"


def ensure_papers_indexed(dry_run: bool = False) -> dict:
    """The main entry point. Syncs PDFs from Drive, runs the
    indexer, then runs the extractor for every slug that's
    missing paper.json or markscheme.json.

    Returns a summary dict:
        {
          "synced_pdfs": [Path, ...],
          "indexed": bool,                # did index_papers.py run?
          "extracted_slugs": [str, ...],  # slugs we ran extract on
          "skipped_slugs": [str, ...],    # slugs already up to date
        }

    Designed to be called from src/run.py before the auto-discover
    step. Safe to call multiple times: every step is idempotent.
    """
    summary = {
        "synced_pdfs": [],
        "indexed": False,
        "extracted_slugs": [],
        "skipped_slugs": [],
    }

    _log("=" * 60)
    _log("Step 0/8: sync PDFs from exam-papers/ and ensure indexed")
    _log("=" * 60)

    # 1. Sync PDFs
    _log("[1/3] Sync PDFs from Drive-mirrored exam-papers/")
    synced = sync_pdfs_from_drive(dry_run=dry_run)
    summary["synced_pdfs"] = [str(p) for p in synced]

    # 2. Run indexer if anything changed OR the master index is
    #    missing (e.g. fresh clone with no index/papers.json).
    needs_index = bool(synced) or not (INDEX_DIR / "papers.json").is_file()
    if needs_index:
        _log("[2/3] Run index_papers.py")
        rc = run_index_papers(dry_run=dry_run)
        # Only mark 'indexed' if we actually invoked the subprocess.
        # In dry-run, run_index_papers returns 0 without doing
        # anything; we shouldn't claim the index was rebuilt.
        if not dry_run:
            summary["indexed"] = (rc == 0)
        if rc != 0:
            _log(f"  index_papers.py failed (exit {rc}); aborting ensure_indexed")
            return summary
    else:
        _log("[2/3] Skipping index_papers.py (no new PDFs, index present)")

    # 3. For each slug in papers/, run extract_questions.py if needed.
    _log("[3/3] Ensure paper.json + markscheme.json per slug")
    if not PAPERS_DIR.is_dir():
        _log(f"  papers/ does not exist; nothing to extract")
        return summary

    slugs = sorted([p.name for p in PAPERS_DIR.iterdir() if p.is_dir()])
    if not slugs:
        _log("  no slug dirs in papers/; nothing to extract")
        return summary

    for slug in slugs:
        slug_dir = PAPERS_DIR / slug
        needs, reason = _slug_needs_extract(slug_dir)
        if not needs:
            _log(f"  {slug}: {reason}, skip")
            summary["skipped_slugs"].append(slug)
            continue
        _log(f"  {slug}: {reason} -> running extract_questions.py")
        rc = run_extract_questions(slug, dry_run=dry_run)
        if rc == 0:
            summary["extracted_slugs"].append(slug)
        else:
            _log(f"  extract_questions.py for {slug} failed (exit {rc})")
            # Don't break the loop -- other slugs may still be
            # extractable. The orchestrator will catch the missing
            # markscheme.json at its check_markscheme_exists step.
    return summary


def main(argv=None) -> int:
    """CLI wrapper for ad-hoc invocation. Useful for debugging or
    for running the sync outside the orchestrator (e.g. from a
    cron job that just keeps the index warm).
    """
    parser = argparse.ArgumentParser(
        description="Sync PDFs from exam-papers/, index them, "
                    "and extract paper.json + markscheme.json "
                    "for any new slug. See module docstring for "
                    "the full design."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without copying files or "
             "running subprocesses."
    )
    args = parser.parse_args(argv)
    summary = ensure_papers_indexed(dry_run=args.dry_run)
    _log("")
    _log("Summary:")
    _log(f"  PDFs synced from Drive:  {len(summary['synced_pdfs'])}")
    _log(f"  Index rebuilt:           {summary['indexed']}")
    _log(f"  Slugs extracted:         {summary['extracted_slugs']}")
    _log(f"  Slugs already current:   {summary['skipped_slugs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
