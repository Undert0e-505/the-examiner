"""
src/ocr_batch.py - thin wrapper around the codex_lane CLI for OCR runs.

This is NOT a standalone Python OCR script. It is a small driver that:

  1. Stages photos from the OpenClaw gateway cache into the repo's
     `intake/<paper-slug>/` directory, named by the printed page number on the paper.
  2. Auto-generates the Codex prompt from the Jinja template
     `src/prompts/ocr.md.j2`, using the page list as the variable input.
     (Per Aaron's policy on 2026-06-15, the per-run human-authored
     prompt is retired; the template is the canonical prompt.)
  3. Invokes the codex_lane PowerShell wrapper to run Codex in a
     disposable sandbox, with the generated prompt. The wrapper runs
     Codex, captures the transcripts, and writes a CODEX_RESULT.md. The
     wrapper never touches the real repo - the OCR pass is sandboxed
     end-to-end.
  4. Copies the produced `*.transcript.md` files from the sandbox back
     to the real repo's `intake/<paper-slug>/` directory, next to the
     photos. Old transcripts at the same paths are preserved as
     `*.transcript.md.bak`.

The script is also importable as a module: the top-level orchestrator
in `src/run.py` calls `run_ocr(slug, page_numbers, page_contexts,
batch_id, ...)` directly, without going through argparse. The CLI is
for manual / debugging use.

Usage (CLI, from the repo root):

    D:\\Python310\\python.exe src/ocr_batch.py ^
        --slug aqa-84621h-chemistry-higher-2024-05 ^
        --job-name ocr-run-aqa-2024-05 ^
        --photos-glob "C:/Users/openclaw-agent/.openclaw/media/inbound/2026-06-14_21-00*.jpg" ^
        --page-order 11 12 14 15 17 19 20 21 22 23 24 25 26 27 28 29 ^
        --yes

The --page-order is the printed page number for each photo, in the
order the photos are in the gateway cache. Gaps (e.g. 13, 16, 18)
are intentional and mean "no answer spaces on that page." Without
--page-order, the script names files by their 1-based index in the
glob, which is wrong for most runs (the photos arrive in
gateway-cache order, not paper-page order, and there are usually
gaps). Use --page-order to be explicit about which photo is which
page.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from generate_prompts import render_ocr_prompt, write_prompt_to_spec_path

REPO_ROOT = Path("D:/dev/the-examiner")
GATEWAY_CACHE = Path("C:/Users/openclaw-agent/.openclaw/media/inbound")
WRAPPER = Path("D:/dev/openclaw-scripts/codex_lane/run_codex_sandbox_job.ps1")


def stage_photos(slug: str, photo_paths: list[Path], page_order: list[int] | None) -> tuple[Path, list[Path]]:
    """Copy photos from the gateway cache to intake/<slug>/NN.jpg.

    Returns (intake_dir, copied_paths). If page_order is given, the N-th photo
    is named after the N-th page number in the list. If page_order is None,
    the N-th photo is named NN.jpg with N being its 1-based index.

    Existing files at the same name are NOT overwritten - the copy fails
    loudly if the destination already has a file. Re-runs should clear the
    intake folder first, or use a new slug.
    """
    intake_dir = REPO_ROOT / "intake" / slug
    intake_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for i, src in enumerate(photo_paths, start=1):
        if page_order is not None:
            if len(page_order) != len(photo_paths):
                raise ValueError(
                    f"--page-order has {len(page_order)} entries but there are "
                    f"{len(photo_paths)} photos. They must match."
                )
            page_num = page_order[i - 1]
            name = f"{page_num:02d}.jpg"
        else:
            name = f"{i:02d}.jpg"
        dest = intake_dir / name
        if dest.exists():
            raise FileExistsError(
                f"{dest} already exists. Clear the intake folder or use a new slug."
            )
        shutil.copy2(src, dest)
        copied.append(dest)
    return intake_dir, copied


def build_prompt(
    slug: str,
    page_numbers: list[int],
    page_contexts: dict[int, str] | None = None,
    batch_id: str | None = None,
) -> Path:
    """Generate the per-run OCR prompt and write it to the codex_lane
    spec path. Returns the path of the written file. Raises if any
    step fails. The orchestrator in src/run.py calls this directly;
    the CLI also calls it.
    """
    content = render_ocr_prompt(
        slug=slug,
        page_numbers=page_numbers,
        page_contexts=page_contexts,
        batch_id=batch_id,
    )
    return write_prompt_to_spec_path(content, slug, "ocr")


def run_codex_lane(
    job_name: str,
    prompt_file: Path,
    *,
    yes: bool,
    progress_interval_sec: int = 60,
) -> subprocess.CompletedProcess:
    """Invoke the codex_lane PowerShell wrapper. The wrapper runs Codex in
    a disposable sandbox, captures the transcripts, and writes CODEX_RESULT.md.

    Returns the CompletedProcess from the PowerShell invocation. The wrapper
    exits 0 on success, non-zero on a sanity-check failure (e.g. secret
    pattern in the source tree, sandbox already exists, etc.).
    """
    if not WRAPPER.exists():
        raise FileNotFoundError(
            f"codex_lane wrapper not found at {WRAPPER}. "
            f"See src/README.md for the dependency."
        )
    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Prompt file not found at {prompt_file}. "
            f"This is a bug in the orchestrator; the prompt should have been "
            f"generated and written before this call."
        )
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


def copy_transcripts_back(sandbox_path: Path, intake_dir: Path) -> list[Path]:
    """Copy *.transcript.md files from the sandbox's intake/<slug>/ to the
    real repo's intake/<slug>/. Existing transcripts at the same paths are
    renamed to *.transcript.md.bak first, so a re-OCR preserves the old
    transcript for diff/recovery.

    The intake slug in the sandbox is the same as in the real repo (the
    wrapper copies the source tree verbatim, with the same relative paths).
    """
    if not sandbox_path.exists():
        raise FileNotFoundError(
            f"Sandbox not found at {sandbox_path}. Did the codex_lane wrapper run?"
        )
    sandbox_intake = sandbox_path / "intake" / intake_dir.name
    if not sandbox_intake.exists():
        raise FileNotFoundError(
            f"Sandbox intake not found at {sandbox_intake}. "
            f"Was the photos-staging step done before the codex_lane run?"
        )
    copied = []
    for src in sorted(sandbox_intake.glob("*.transcript.md")):
        dest = intake_dir / src.name
        if dest.exists():
            bak = dest.with_suffix(".transcript.md.bak")
            print(f"Backing up existing {dest} to {bak}", flush=True)
            shutil.move(dest, bak)
        shutil.copy2(src, dest)
        copied.append(dest)
    return copied


def run_ocr(
    slug: str,
    job_name: str,
    photo_paths: list[Path],
    page_order: list[int] | None = None,
    page_contexts: dict[int, str] | None = None,
    batch_id: str | None = None,
    *,
    yes: bool = False,
    progress_interval_sec: int = 60,
    skip_copy_back: bool = False,
) -> dict:
    """End-to-end OCR pass: stage photos, build the prompt, run codex_lane,
    copy transcripts back. Returns a dict with the run's artifacts.

    This is the function the orchestrator in src/run.py calls. The CLI
    just wraps it with argparse.

    Returns:
        {
            "intake_dir": Path,
            "copied_photos": list[Path],
            "prompt_file": Path,
            "codex_returncode": int,
            "transcripts_copied_back": list[Path] | None,
        }
    """
    intake_dir, copied_photos = stage_photos(slug, photo_paths, page_order)
    print(f"Staged {len(copied_photos)} photos into {intake_dir}", flush=True)

    page_numbers = page_order if page_order is not None else list(range(1, len(photo_paths) + 1))
    prompt_file = build_prompt(slug, page_numbers, page_contexts, batch_id)
    print(f"Wrote OCR prompt to {prompt_file}", flush=True)

    codex = run_codex_lane(
        job_name,
        prompt_file,
        yes=yes,
        progress_interval_sec=progress_interval_sec,
    )
    if codex.returncode != 0:
        return {
            "intake_dir": intake_dir,
            "copied_photos": copied_photos,
            "prompt_file": prompt_file,
            "codex_returncode": codex.returncode,
            "transcripts_copied_back": None,
        }

    transcripts_copied_back = None
    if not skip_copy_back:
        sandbox_path = Path("D:/dev/codex-sandboxes") / job_name
        transcripts_copied_back = copy_transcripts_back(sandbox_path, intake_dir)
        print(f"Copied {len(transcripts_copied_back)} transcripts back", flush=True)
    return {
        "intake_dir": intake_dir,
        "copied_photos": copied_photos,
        "prompt_file": prompt_file,
        "codex_returncode": codex.returncode,
        "transcripts_copied_back": transcripts_copied_back,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Stage photos, run Codex in a disposable sandbox for OCR, and copy "
            "transcripts back. The prompt is auto-generated from "
            "src/prompts/ocr.md.j2 + the page list."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--slug",
        required=True,
        help="Paper slug, e.g. 'aqa-84621h-chemistry-higher-2024-05'.",
    )
    p.add_argument(
        "--job-name",
        required=True,
        help="Name of the Codex-sandbox job, e.g. 'ocr-run-aqa-2024-05'.",
    )
    p.add_argument(
        "--photos-glob",
        required=True,
        help="Glob pattern for the photos in the gateway cache.",
    )
    p.add_argument(
        "--page-order",
        type=int,
        nargs="*",
        default=None,
        help="Printed page number for each photo, in glob order.",
    )
    p.add_argument(
        "--page-contexts",
        default=None,
        help="JSON dict {page_num: 'short context'} for the OCR prompt. Optional.",
    )
    p.add_argument(
        "--batch-id",
        default=None,
        help="OCR batch label, e.g. 'A' / 'B' / 'C' for split batches. Optional.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Pass -Yes to the codex_lane wrapper, skipping its interactive confirmations.",
    )
    p.add_argument(
        "--progress-interval-sec",
        type=int,
        default=60,
        help="Heartbeat interval for the wrapper. Default 60.",
    )
    p.add_argument(
        "--skip-copy-back",
        action="store_true",
        help="If set, don't copy the produced transcripts back from the sandbox.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Resolve the glob.
    glob_pattern = args.photos_glob
    if Path(glob_pattern).is_absolute():
        search_root = Path(glob_pattern).parent
        pattern = Path(glob_pattern).name
    else:
        search_root = GATEWAY_CACHE
        pattern = glob_pattern
    photo_paths = sorted(search_root.glob(pattern))
    if not photo_paths:
        print(f"No photos matched {args.photos_glob}", file=sys.stderr)
        return 1
    print(f"Found {len(photo_paths)} photos", flush=True)

    page_contexts = None
    if args.page_contexts:
        page_contexts = json.loads(args.page_contexts)

    result = run_ocr(
        slug=args.slug,
        job_name=args.job_name,
        photo_paths=photo_paths,
        page_order=args.page_order,
        page_contexts=page_contexts,
        batch_id=args.batch_id,
        yes=args.yes,
        progress_interval_sec=args.progress_interval_sec,
        skip_copy_back=args.skip_copy_back,
    )
    return result["codex_returncode"]


if __name__ == "__main__":
    sys.exit(main())
