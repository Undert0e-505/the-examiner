"""
src/discover_batch.py - paper-and-page-order discovery from a batch
                          of Telegram photos.

This is the new front-end for the auto-pipeline. It replaces the
manual "Aaron types /mark <slug> photos=N order=..." step with an
automated discovery pass.

The flow is:

  1. Caller hands us a list of photo paths (the most recent batch
     in the gateway cache). The order is the receipt order from
     the gateway (oldest-first by LastWriteTime).
  2. We stage the photos into a temporary intake location:
     `intake/_discover/<job-name>/NN.jpg` where NN is the 1-based
     receipt order.
  3. We build a small, fast Codex prompt (src/prompts/discover.md.j2)
     that asks Codex to:
       - read the paper code off the cover (file index 1)
       - read the printed page number off each photo
       - read the question numbers on each photo (best-effort)
       - write a single `intake/DISCOVERY.json` with the results
  4. We invoke the codex_lane wrapper. Codex runs in a disposable
     sandbox, reads the photos, writes DISCOVERY.json, and exits.
  5. We read DISCOVERY.json from the sandbox. We match the paper
     code against the existing `papers/*/pair.json` files to get
     the canonical slug. If no match, we abort with a clear error
     (no point asking Codex to OCR a paper that isn't in the repo).
  6. We return the discovered slug + page_order list. The
     orchestrator (src/run.py) renames the staged photos in the
     real repo's `intake/_discover/<job>/` to
     `intake/<slug>/<page>.jpg`, then runs the normal OCR +
     marking + publish + email pipeline using the discovered
     values. The discovery pass never moves files in the sandbox
     or the real repo itself - that's the orchestrator's job.

The discovery pass is intentionally small and fast. It does NOT
OCR the student's answers - that is the next pass. The goal is
to identify the paper and the page order with as little Codex
work as possible.

Usage (CLI, from the repo root):

    D:\\Python310\\python.exe src/discover_batch.py ^
        --photos-glob "C:/Users/openclaw-agent/.openclaw/media/inbound/2026-06-15_10-26*.jpg" ^
        --job-name discover-2026-06-15 ^
        --yes

The orchestrator in src/run.py calls this directly via
`discover_batch(photo_paths, job_name, ...)`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from generate_prompts import render_discover_prompt, write_prompt_to_spec_path

REPO_ROOT = Path("D:/dev/the-examiner")
GATEWAY_CACHE = Path("C:/Users/openclaw-agent/.openclaw/media/inbound")
WRAPPER = Path("D:/dev/openclaw-scripts/codex_lane/run_codex_sandbox_job.ps1")
DISCOVER_INTAKE_SLUG = "_discover"  # special: not a real paper slug


# ---------- Paper code matching ----------

def _normalize_paper_code(raw: str) -> str:
    """Normalise a paper code string for matching.

    Strips whitespace, uppercases, removes spaces around slashes,
    keeps the alphanumeric characters. Used to compare what Codex
    read off the cover against the spec/paper combo in pair.json.

    Examples:
        "8462/1H"      -> "8462/1H"
        "8462 / 1H"    -> "8462/1H"
        "1MA1 1H"      -> "1MA11H"  (concat of spec+paper for Edexcel)
    """
    return re.sub(r"\s+", "", raw.strip().upper())


def _load_pair_index() -> dict:
    """Index all known papers in the repo by normalised paper code.

    Returns a dict:
        {
          "8462/1H": [("aqa-84621h-chemistry-higher-2024-05", pair.json dict), ...],
          "1MA11H":  [("edexcel-1ma11h-mathematics-higher-2024-11", pair.json dict), ...],
          ...
        }

    The list is non-empty for a normal repo but can have multiple
    entries (e.g. if a paper was indexed twice). The caller picks
    the first match.

    The keys are the spec+paper combos, normalised the same way
    Codex's read is normalised. This handles the common case
    where Codex reads "8462/1H" from the cover and we have
    "8462" + "1H" in pair.json.
    """
    papers_dir = REPO_ROOT / "papers"
    if not papers_dir.is_dir():
        return {}
    index: dict[str, list[tuple[str, dict]]] = {}
    for pair_path in papers_dir.glob("*/pair.json"):
        try:
            data = json.loads(pair_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARN: failed to read {pair_path}: {e}", flush=True)
            continue
        spec = data.get("spec", "").upper()
        paper = data.get("paper", "").upper()
        if not spec or not paper:
            continue
        # Try the canonical form first: "<spec>/<paper>" (AQA style)
        # and the Edexcel/OCR style: "<spec><paper>" concatenated.
        candidates = [
            f"{spec}/{paper}",
            f"{spec}{paper}",
        ]
        slug = data.get("slug") or pair_path.parent.name
        for key in candidates:
            index.setdefault(_normalize_paper_code(key), []).append((slug, data))
    return index


def match_paper_code(cover_paper_code: str, cover_text: str = "") -> tuple[str, dict] | None:
    """Match a paper code read off the cover (or a longer cover
    text string) to a known paper in the repo.

    Returns (slug, pair_data) on a match, or None if no match.

    Matching strategy:
      1. Try direct lookup of the normalised paper code.
      2. If no match, try a substring search of the spec + paper
         in the cover_text (handles Codex reading "8462" + "1H"
         separately, or being verbose on the cover).
    """
    index = _load_pair_index()
    code = _normalize_paper_code(cover_paper_code or "")
    if code in index and index[code]:
        slug, data = index[code][0]
        return slug, data
    # Fallback: substring search in cover text. Try both the
    # canonical "<spec>/<paper>" form (AQA style) and the joined
    # "<spec><paper>" form (Edexcel/OCR style), and also try
    # the spec alone (Codex might read the spec and paper as
    # separate tokens).
    if cover_text:
        normalized_text = _normalize_paper_code(cover_text)
        for key, hits in index.items():
            if key in normalized_text:
                slug, data = hits[0]
                return slug, data
        # Also try a spec-only match: e.g. cover text contains
        # "8462" but Codex missed the "/1H" part. Look for any
        # spec in the text and use the first paper that matches.
        for pair_path in (REPO_ROOT / "papers").glob("*/pair.json"):
            try:
                d = json.loads(pair_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            spec = d.get("spec", "").upper()
            paper = d.get("paper", "").upper()
            if spec and spec in normalized_text:
                # Spec matched; check if paper is also in the text
                # (joined or slashed).
                if paper and (
                    f"{spec}/{paper}" in normalized_text
                    or f"{spec}{paper}" in normalized_text
                ):
                    return d.get("slug") or pair_path.parent.name, d
    return None


# ---------- Photo staging ----------

def stage_photos_for_discovery(photo_paths: list[Path], job_name: str) -> tuple[Path, list[Path]]:
    """Copy photos to intake/_discover/<job>/NN.jpg (NN = 1-based
    receipt order). Returns (intake_dir, copied_paths).

    Unlike the regular OCR staging, we don't use page numbers here
    because we don't know them yet. We name files by receipt order
    so the discovery prompt can reference them by file index
    (which is what gets recorded in DISCOVERY.json).
    """
    intake_dir = REPO_ROOT / "intake" / DISCOVER_INTAKE_SLUG / job_name
    if intake_dir.exists():
        shutil.rmtree(intake_dir)
    intake_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for i, src in enumerate(photo_paths, start=1):
        if not src.is_file():
            raise FileNotFoundError(f"photo not found: {src}")
        dest = intake_dir / f"{i:02d}.jpg"
        shutil.copy2(src, dest)
        copied.append(dest)
    return intake_dir, copied


# ---------- Codex run ----------

def run_codex_lane(
    job_name: str,
    prompt_file: Path,
    *,
    yes: bool,
    progress_interval_sec: int = 60,
) -> subprocess.CompletedProcess:
    """Same wrapper invocation as ocr_batch.run_codex_lane. Kept
    here so discover_batch is self-contained - the orchestrator
    can call discover_batch without importing ocr_batch."""
    if not WRAPPER.exists():
        raise FileNotFoundError(
            f"codex_lane wrapper not found at {WRAPPER}. "
            f"See src/README.md for the dependency."
        )
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found at {prompt_file}.")
    cmd = [
        "powershell",
        "-ExecutionPolicy", "Bypass",
        "-File", str(WRAPPER),
        "-SourceRepo", str(REPO_ROOT),
        "-JobName", job_name,
        "-PromptFile", str(prompt_file),
        "-UseCopy",
        "-Yes",
        "-ProgressIntervalSec", str(progress_interval_sec),
    ]
    print(f"About to run: {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=False)


def read_discovery_json(sandbox_path: Path) -> dict:
    """Read intake/DISCOVERY.json from the sandbox. Raises
    FileNotFoundError if it's missing (Codex failed to write it)."""
    discovery_path = sandbox_path / "intake" / "DISCOVERY.json"
    if not discovery_path.is_file():
        raise FileNotFoundError(
            f"DISCOVERY.json not found at {discovery_path}. "
            f"Codex may have failed to write the discovery result. "
            f"Check the sandbox's CODEX_RESULT.md for details."
        )
    return json.loads(discovery_path.read_text(encoding="utf-8"))


# ---------- Real-repo staging after discovery ----------

def restage_real_repo_after_discovery(
    job_name: str,
    slug: str,
    page_numbers_by_index: dict[int, int | None],
) -> list[Path]:
    """After the Codex discovery run, rename the photos in the
    REAL repo (not the sandbox) from
    `intake/_discover/<job>/NN.jpg` to `intake/<slug>/<page>.jpg`,
    where <page> is the discovered printed page number. The
    existing OCR pass (with --skip-staging) then picks up these
    files unchanged.

    Photos whose page number couldn't be determined are named
    `unknown-NN.jpg` and excluded from the page_order list.
    Returns the list of new paths in `intake/<slug>/`.
    """
    src_dir = REPO_ROOT / "intake" / DISCOVER_INTAKE_SLUG / job_name
    if not src_dir.is_dir():
        raise FileNotFoundError(
            f"discovery intake not found: {src_dir}. "
            f"Did the discovery pass stage the photos?"
        )
    dest_dir = REPO_ROOT / "intake" / slug
    if dest_dir.exists() and any(dest_dir.glob("*.jpg")):
        raise FileExistsError(
            f"{dest_dir} already contains .jpg files. "
            f"Clear the intake folder or use a new slug."
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_paths = []
    for src in sorted(src_dir.glob("*.jpg")):
        m = re.match(r"(\d+)\.jpg$", src.name)
        if not m:
            raise ValueError(f"unexpected photo name: {src.name}")
        idx = int(m.group(1))
        page = page_numbers_by_index.get(idx)
        if page is None:
            new_name = f"unknown-{idx:02d}.jpg"
            print(f"WARN: photo index {idx} has no page number; naming {new_name}", flush=True)
        else:
            new_name = f"{page:02d}.jpg"
        dest = dest_dir / new_name
        shutil.move(str(src), str(dest))
        new_paths.append(dest)
    return new_paths


def cleanup_discovery_intake(job_name: str) -> None:
    """Delete the temporary intake/_discover/<job>/ folder after
    the orchestrator has restaged the photos. Best-effort: silent
    if the folder doesn't exist."""
    src_dir = REPO_ROOT / "intake" / DISCOVER_INTAKE_SLUG / job_name
    if src_dir.is_dir():
        shutil.rmtree(src_dir, ignore_errors=True)
    # Also remove the now-empty _discover parent if it has no other jobs
    parent = REPO_ROOT / "intake" / DISCOVER_INTAKE_SLUG
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


# ---------- Public API ----------

def discover_batch(
    photo_paths: list[Path],
    job_name: str = "discover-batch",
    *,
    yes: bool = False,
    progress_interval_sec: int = 60,
) -> dict:
    """End-to-end discovery: stage photos, run Codex, parse the
    result, return the discovered slug + page order. The caller
    is responsible for renaming the staged photos in the real
    repo (via restage_real_repo_after_discovery) before running
    the OCR pass.

    Returns a dict:
        {
          "slug": str,
          "pair": dict (the pair.json contents for the matched paper),
          "page_order": list[int],  # in receipt order: the printed
                                     # page number of each photo
          "page_numbers": dict[int, int | None],  # file_index -> page
          "cover_paper_code": str,
          "cover_text": str,
          "question_numbers": dict[int, list[str]],
          "confidence": str,
          "intake_dir": Path,  # real-repo intake dir
          "job_name": str,    # for restage_real_repo_after_discovery
        }

    Raises:
        ValueError: if no slug match is found (paper not in repo).
        FileNotFoundError: if Codex doesn't write DISCOVERY.json.
    """
    if not photo_paths:
        raise ValueError("photo_paths must be non-empty")

    # 1. Stage photos in the real repo
    intake_dir, copied = stage_photos_for_discovery(photo_paths, job_name)
    print(f"Staged {len(copied)} photos into {intake_dir}", flush=True)

    # 2. Build prompt. Codex reads the photos from inside the
    # sandbox at the relative path where the wrapper put them
    # (the wrapper's -UseCopy copies the source repo verbatim,
    # so intake/_discover/<job>/NN.jpg is in the sandbox too).
    photos_for_prompt = [
        {
            "path": f"intake/{DISCOVER_INTAKE_SLUG}/{job_name}/{i+1:02d}.jpg",
            "index": i + 1,
        }
        for i in range(len(copied))
    ]
    content = render_discover_prompt(photos=photos_for_prompt)
    prompt_file = write_prompt_to_spec_path(content, "_", "discover")
    print(f"Wrote discovery prompt to {prompt_file}", flush=True)

    # 3. Run Codex
    codex = run_codex_lane(
        job_name,
        prompt_file,
        yes=yes,
        progress_interval_sec=progress_interval_sec,
    )
    if codex.returncode != 0:
        raise RuntimeError(
            f"codex_lane exit {codex.returncode}; discovery failed. "
            f"Check the sandbox's CODEX_RESULT.md."
        )

    # 4. Read DISCOVERY.json from the sandbox
    sandbox_path = Path("D:/dev/codex-sandboxes") / job_name
    discovery = read_discovery_json(sandbox_path)

    cover_paper_code = discovery.get("cover_paper_code", "")
    cover_text = discovery.get("cover_text", "")
    page_numbers_raw = discovery.get("page_numbers", {})
    question_numbers = discovery.get("question_numbers", {})

    # 5. Match paper code
    match = match_paper_code(cover_paper_code, cover_text)
    if match is None:
        raise ValueError(
            f"Discovered paper code {cover_paper_code!r} (from cover text "
            f"{cover_text!r}) does not match any known paper in the repo. "
            f"Run `python src/index_papers.py` and add the paper to "
            f"`papers/<slug>/` before retrying."
        )
    slug, pair = match
    print(f"Discovered slug: {slug}", flush=True)

    # 6. Build the page_numbers dict (file_index -> page)
    page_numbers: dict[int, int | None] = {}
    for k, v in page_numbers_raw.items():
        idx = int(k)
        if v is None:
            page_numbers[idx] = None
        else:
            try:
                page_numbers[idx] = int(v)
            except (TypeError, ValueError):
                page_numbers[idx] = None
    # Build page_order in receipt order (file_index 1..N)
    page_order: list[int] = []
    for idx in sorted(page_numbers.keys()):
        p = page_numbers[idx]
        if p is not None:
            page_order.append(p)
        else:
            # We don't know the page number; skip from page_order.
            # (The orchestrator may abort or warn.)
            pass

    return {
        "slug": slug,
        "pair": pair,
        "page_order": page_order,
        "page_numbers": page_numbers,
        "cover_paper_code": cover_paper_code,
        "cover_text": cover_text,
        "question_numbers": question_numbers,
        "confidence": discovery.get("confidence", "unknown"),
        "intake_dir": intake_dir,
        "job_name": job_name,
    }


# ---------- CLI ----------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Discover the paper slug and printed page order from a batch of "
            "Telegram photos. Used by the auto-pipeline's auto-discover mode."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--photos",
        nargs="*",
        type=Path,
        required=True,
        help="Photo paths in receipt order (the order the gateway cached them).",
    )
    p.add_argument(
        "--job-name", default="discover-batch",
        help="Codex-sandbox job name (default: discover-batch).",
    )
    p.add_argument(
        "--restage", action="store_true",
        help="After discovery, rename the photos in the real repo's "
             "intake/_discover/<job>/ to intake/<slug>/<page>.jpg. "
             "Use this when running discover_batch standalone to "
             "prep the intake for a subsequent OCR pass.",
    )
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.yes:
        print(f"This will run Codex in a disposable sandbox to discover the", flush=True)
        print(f"paper slug + page order for {len(args.photos)} photos.", flush=True)
        print(f"  photos: {len(args.photos)}", flush=True)
        print(f"  job name: {args.job_name}", flush=True)
        resp = input("Continue? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1
    result = discover_batch(
        photo_paths=args.photos,
        job_name=args.job_name,
        yes=args.yes,
    )
    print("", flush=True)
    print("=" * 60, flush=True)
    print("DISCOVERY RESULT", flush=True)
    print("=" * 60, flush=True)
    print(f"  slug: {result['slug']}", flush=True)
    print(f"  cover_paper_code: {result['cover_paper_code']}", flush=True)
    print(f"  cover_text: {result['cover_text']}", flush=True)
    print(f"  page_order: {result['page_order']}", flush=True)
    print(f"  confidence: {result['confidence']}", flush=True)
    if args.restage:
        new_paths = restage_real_repo_after_discovery(
            job_name=args.job_name,
            slug=result["slug"],
            page_numbers_by_index=result["page_numbers"],
        )
        print(f"  restaged {len(new_paths)} photos to intake/{result['slug']}/", flush=True)
        cleanup_discovery_intake(args.job_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
