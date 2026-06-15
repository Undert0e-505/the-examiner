"""
src/mark_batch.py - thin wrapper around the codex_lane CLI for marking runs.

This is NOT a standalone Python marking script. It is a small driver that:

  1. Verifies that `intake/<paper-slug>/NN.transcript.md` files exist
     (the OCR pass must have completed first - run the orchestrator or
     `ocr_batch.py` first if it hasn't). It also verifies that
     `papers/<paper-slug>/markscheme.json` exists, since the marking
     prompt needs the rubric as input.
  2. Auto-generates the Codex prompt from the Jinja template
     `src/prompts/mark.md.j2`, with the per-paper metadata from the
     markscheme and the transcript list. (Per Aaron's policy on
     2026-06-15, the per-run human-authored prompt is retired; the
     template is the canonical prompt.)
  3. Invokes the codex_lane PowerShell wrapper to run Codex in a
     disposable sandbox, with the generated prompt. The wrapper runs
     Codex, captures the per-question marking files + SUMMARY.md, and
     writes a CODEX_RESULT.md. The wrapper never touches the real repo -
     the marking pass is sandboxed end-to-end.
  4. Copies the produced `Q*.marking.md` and `SUMMARY.md` files from
     the sandbox back to the real repo's `assessments/<paper-slug>/`
     directory. Old marking files at the same paths are preserved as
     `*.marking.md.bak`.

The script is also importable as a module: the top-level orchestrator
in `src/run.py` calls `run_marking(slug, ...)` directly, without going
through argparse. The CLI is for manual / debugging use.

Usage (CLI, from the repo root):

    D:\\Python310\\python.exe src/mark_batch.py ^
        --slug aqa-84621h-chemistry-higher-2024-05 ^
        --job-name mark-run-aqa-2024-05 ^
        --yes

The script does NOT need --page-order or --photos-glob - the marking
pass reads the transcripts (which are already in the repo) and the
markscheme (which is also already in the repo at
papers/<slug>/markscheme.json). All the staging has happened by this
point; the only thing left is to invoke Codex with the marking prompt
and copy the marking files back.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from generate_prompts import render_mark_prompt, write_prompt_to_spec_path

REPO_ROOT = Path("D:/dev/the-examiner")
WRAPPER = Path("D:/dev/openclaw-scripts/codex_lane/run_codex_sandbox_job.ps1")


def verify_inputs(slug: str) -> tuple[Path, Path, list[Path]]:
    """Verify that the OCR pass has produced transcripts and that the markscheme
    is in the repo. Returns (intake_dir, markscheme_path, transcript_paths).

    Raises FileNotFoundError with a clear message if anything is missing.
    """
    intake_dir = REPO_ROOT / "intake" / slug
    if not intake_dir.is_dir():
        raise FileNotFoundError(
            f"intake/{slug}/ does not exist. Run ocr_batch.py first, or check "
            f"the slug - it must match an existing directory in intake/."
        )
    transcripts = sorted(intake_dir.glob("*.transcript.md"))
    if not transcripts:
        raise FileNotFoundError(
            f"intake/{slug}/ has no *.transcript.md files. The OCR pass must "
            f"have produced transcripts before the marking pass can run. "
            f"Run ocr_batch.py first."
        )
    markscheme_path = REPO_ROOT / "papers" / slug / "markscheme.json"
    if not markscheme_path.is_file():
        raise FileNotFoundError(
            f"papers/{slug}/markscheme.json does not exist. The Phase 2 "
            f"extractor must have produced a markscheme for this paper. "
            f"Run `python src/extract_questions.py --slug {slug}` to produce "
            f"it, or check the slug for typos."
        )
    return intake_dir, markscheme_path, transcripts


def build_prompt(slug: str) -> Path:
    """Generate the per-run mark prompt and write it to the codex_lane
    spec path. Returns the path of the written file. The orchestrator
    in src/run.py calls this directly; the CLI also calls it.
    """
    content = render_mark_prompt(slug=slug)
    return write_prompt_to_spec_path(content, slug, "mark")


def run_codex_lane(
    job_name: str,
    prompt_file: Path,
    *,
    yes: bool,
    progress_interval_sec: int = 60,
) -> subprocess.CompletedProcess:
    """Invoke the codex_lane PowerShell wrapper. See ocr_batch.py for the
    full design - this is the same wrapper, just with a different prompt.
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


def copy_marking_back(sandbox_path: Path, slug: str) -> list[Path]:
    """Copy Q*.marking.md and SUMMARY.md files from the sandbox's
    assessments/<slug>/ to the real repo's assessments/<slug>/. Existing
    marking files at the same paths are renamed to *.marking.md.bak first.

    The assessments slug in the sandbox is the same as in the real repo.
    The real repo's assessments/<slug>/ directory is created if it doesn't
    exist (the marking pass is the first thing to land there).
    """
    if not sandbox_path.exists():
        raise FileNotFoundError(
            f"Sandbox not found at {sandbox_path}. Did the codex_lane wrapper run?"
        )
    sandbox_assessments = sandbox_path / "assessments" / slug
    if not sandbox_assessments.exists():
        raise FileNotFoundError(
            f"Sandbox assessments not found at {sandbox_assessments}. "
            f"Was the prompt told to write to assessments/{slug}/?"
        )
    real_assessments = REPO_ROOT / "assessments" / slug
    real_assessments.mkdir(parents=True, exist_ok=True)
    copied = []
    # Match Q01.marking.md, Q02.marking.md, ..., QNN.marking.md, and SUMMARY.md.
    patterns = ["Q*.marking.md", "SUMMARY.md"]
    for pattern in patterns:
        for src in sorted(sandbox_assessments.glob(pattern)):
            dest = real_assessments / src.name
            if dest.exists():
                bak = dest.with_suffix(dest.suffix + ".bak")
                print(f"Backing up existing {dest} to {bak}", flush=True)
                shutil.move(dest, bak)
            shutil.copy2(src, dest)
            copied.append(dest)
    return copied


def parse_marks_from_summary(summary_path: Path) -> dict[str, int] | None:
    """Best-effort parse of the SUMMARY.md to extract the per-Q tally and the
    total marks awarded. Returns a dict like
    {'total_awarded': 70, 'total_available': 100, 'Q1': 8, 'Q2': 11, ...}
    or None if the SUMMARY.md doesn't have the expected shape.
    """
    if not summary_path.is_file():
        return None
    content = summary_path.read_text(encoding="utf-8")
    result: dict[str, int] = {}
    m = re.search(r"Total marks awarded:\s*(\d+)", content)
    if m:
        result["total_awarded"] = int(m.group(1))
    m = re.search(r"Total marks available:\s*(\d+)", content)
    if m:
        result["total_available"] = int(m.group(1))
    for m in re.finditer(r"\|\s*Q(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", content):
        q_num = int(m.group(1))
        available = int(m.group(2))
        awarded = int(m.group(3))
        result[f"Q{q_num}"] = awarded
        result[f"Q{q_num}_available"] = available
    return result or None


def run_marking(
    slug: str,
    job_name: str,
    *,
    yes: bool = False,
    progress_interval_sec: int = 60,
    skip_copy_back: bool = False,
) -> dict:
    """End-to-end marking pass: verify inputs, build the prompt, run
    codex_lane, copy marking files back. Returns a dict with the run's
    artifacts. The orchestrator in src/run.py calls this directly; the
    CLI just wraps it with argparse.

    Returns:
        {
            "intake_dir": Path,
            "markscheme_path": Path,
            "transcripts": list[Path],
            "prompt_file": Path,
            "codex_returncode": int,
            "marking_files_copied_back": list[Path] | None,
            "tally": dict | None,
        }
    """
    intake_dir, markscheme_path, transcripts = verify_inputs(slug)
    print(f"  intake/{slug}/ has {len(transcripts)} transcripts", flush=True)
    print(f"  papers/{slug}/markscheme.json is in place", flush=True)

    prompt_file = build_prompt(slug)
    print(f"Wrote marking prompt to {prompt_file}", flush=True)

    codex = run_codex_lane(
        job_name,
        prompt_file,
        yes=yes,
        progress_interval_sec=progress_interval_sec,
    )
    marking_files_copied_back = None
    if codex.returncode == 0 and not skip_copy_back:
        sandbox_path = Path("D:/dev/codex-sandboxes") / job_name
        marking_files_copied_back = copy_marking_back(sandbox_path, slug)
        print(f"Copied {len(marking_files_copied_back)} marking files back", flush=True)
    return {
        "intake_dir": intake_dir,
        "markscheme_path": markscheme_path,
        "transcripts": transcripts,
        "prompt_file": prompt_file,
        "codex_returncode": codex.returncode,
        "marking_files_copied_back": marking_files_copied_back,
        "tally": parse_marks_from_summary(REPO_ROOT / "assessments" / slug / "SUMMARY.md"),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run Codex in a disposable sandbox for per-question marking, then "
            "copy the marking files back. The prompt is auto-generated from "
            "src/prompts/mark.md.j2 + the per-paper markscheme + the transcript list."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--slug", required=True, help="Paper slug.")
    p.add_argument("--job-name", required=True, help="Name of the Codex-sandbox job.")
    p.add_argument("--yes", action="store_true", help="Pass -Yes to the codex_lane wrapper.")
    p.add_argument("--progress-interval-sec", type=int, default=60, help="Heartbeat interval.")
    p.add_argument("--skip-copy-back", action="store_true", help="Don't copy marking files back.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_marking(
        slug=args.slug,
        job_name=args.job_name,
        yes=args.yes,
        progress_interval_sec=args.progress_interval_sec,
        skip_copy_back=args.skip_copy_back,
    )
    tally = result.get("tally")
    if tally is not None:
        print("", flush=True)
        print("Per-question tally (parsed from SUMMARY.md):", flush=True)
        total_awarded = tally.get("total_awarded", "?")
        total_available = tally.get("total_available", "?")
        print(f"  Total: {total_awarded} / {total_available}", flush=True)
        q_keys = sorted(
            [k for k in tally if k.startswith("Q") and not k.endswith("_available") and k[1:].isdigit()],
            key=lambda x: int(x[1:]),
        )
        for k in q_keys:
            print(f"  {k}: {tally[k]} / {tally.get(k + '_available', '?')}", flush=True)
    return result["codex_returncode"]


if __name__ == "__main__":
    sys.exit(main())
