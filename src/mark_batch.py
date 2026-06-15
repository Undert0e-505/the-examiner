"""
src/mark_batch.py - thin wrapper around the codex_lane CLI for marking runs.

This is NOT a standalone Python marking script. It is a small driver that:

  1. Verifies that `intake/<paper-slug>/NN.transcript.md` files exist (the OCR pass must have completed first - run `ocr_batch.py` first if it hasn't). It also verifies that `papers/<paper-slug>/markscheme.json` exists, since the marking prompt needs the rubric as input.
  2. Reads an already-written prompt from `<spec>/04_CODEX_PROMPT.md` in the codex_sandboxes spec tree. (The script does NOT generate the prompt automatically - that is the safety design. The prompt is the IP and the human writes it, with the template in `docs/MARKING-PROMPT-TEMPLATE.md` as a guide, before each run.)
  3. Invokes the codex_lane PowerShell wrapper to run Codex in a disposable sandbox, with the prompt from step 2. The wrapper runs Codex, captures the per-question marking files + SUMMARY.md, and writes a CODEX_RESULT.md. The wrapper never touches the real repo - the marking pass is sandboxed end-to-end.
  4. Copies the produced `Q*.marking.md` and `SUMMARY.md` files from the sandbox back to the real repo's `assessments/<paper-slug>/` directory. Old marking files at the same paths are preserved as `*.marking.md.bak`.

For the design of why this is a thin driver and not a self-contained script, see `src/README.md`. For the prompt template, see `docs/MARKING-PROMPT-TEMPLATE.md`. For the operational doc from a real run, see `assessments/aqa-84621h-chemistry-higher-2024-05/README.md`.

Usage (from the repo root):

    D:\\Python310\\python.exe src/mark_batch.py ^
        --slug aqa-84621h-chemistry-higher-2024-05 ^
        --job-name mark-run-aqa-2024-05 ^
        --prompt-file D:/dev/codex-sandboxes/_specs/mark-run-aqa-2024-05/04_CODEX_PROMPT.md ^
        --yes

The script does NOT need --page-order or --photos-glob - the marking pass reads the transcripts (which are already in the repo) and the markscheme (which is also already in the repo at papers/<slug>/markscheme.json). All the staging has happened by this point; the only thing left is to invoke Codex with the marking prompt and copy the marking files back.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path("D:/dev/the-examiner")
SPECS_ROOT = Path("D:/dev/codex-sandboxes/_specs")
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
            f"Write the prompt by hand (or copy from docs/MARKING-PROMPT-TEMPLATE.md) "
            f"before running this script."
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

    This is for the operator's convenience (so they don't have to open the
    file) - the marking pass itself doesn't depend on this.
    """
    if not summary_path.is_file():
        return None
    content = summary_path.read_text(encoding="utf-8")
    result: dict[str, int] = {}
    # Total marks awarded: "Total marks awarded: 70"
    m = re.search(r"Total marks awarded:\s*(\d+)", content)
    if m:
        result["total_awarded"] = int(m.group(1))
    # Total marks available: "Total marks available: 100"
    m = re.search(r"Total marks available:\s*(\d+)", content)
    if m:
        result["total_available"] = int(m.group(1))
    # Per-Q tally from the table: "| Q1 | 10 | 8 |" -> Q1: 8
    for m in re.finditer(r"\|\s*Q(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", content):
        q_num = int(m.group(1))
        available = int(m.group(2))
        awarded = int(m.group(3))
        result[f"Q{q_num}"] = awarded
        result[f"Q{q_num}_available"] = available
    return result or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run Codex in a disposable sandbox for per-question marking, then "
            "copy the marking files back. The prompt is read from --prompt-file "
            "(write it by hand, or copy from docs/MARKING-PROMPT-TEMPLATE.md)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--slug",
        required=True,
        help="Paper slug, e.g. 'aqa-84621h-chemistry-higher-2024-05'. "
             "The marking pass reads from intake/<slug>/ and writes to "
             "assessments/<slug>/.",
    )
    p.add_argument(
        "--job-name",
        required=True,
        help="Name of the Codex-sandbox job, e.g. 'mark-run-aqa-2024-05'.",
    )
    p.add_argument(
        "--prompt-file",
        type=Path,
        required=True,
        help="Path to the 04_CODEX_PROMPT.md file. Write this by hand before "
             "running, or copy from docs/MARKING-PROMPT-TEMPLATE.md.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Pass -Yes to the codex_lane wrapper.",
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
        help="If set, don't copy the produced marking files back. Useful for "
             "dry-runs.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Step 1: verify the OCR pass and the markscheme are in place.
    print(f"Verifying inputs for {args.slug} ...", flush=True)
    intake_dir, markscheme_path, transcripts = verify_inputs(args.slug)
    print(f"  intake/{args.slug}/ has {len(transcripts)} transcripts", flush=True)
    print(f"  papers/{args.slug}/markscheme.json is in place", flush=True)

    # Step 2: run Codex in a disposable sandbox.
    print(f"Running Codex in sandbox '{args.job_name}' ...", flush=True)
    result = run_codex_lane(
        args.job_name,
        args.prompt_file,
        yes=args.yes,
        progress_interval_sec=args.progress_interval_sec,
    )
    if result.returncode != 0:
        print(f"codex_lane wrapper exited with code {result.returncode}", file=sys.stderr)
        return result.returncode

    # Step 3: copy marking files back.
    if args.skip_copy_back:
        print("Skipping copy-back (--skip-copy-back).", flush=True)
        return 0
    sandbox_path = Path("D:/dev/codex-sandboxes") / args.job_name
    print(f"Copying marking files from {sandbox_path} back ...", flush=True)
    copied_back = copy_marking_back(sandbox_path, args.slug)
    print(f"Copied {len(copied_back)} marking files back:", flush=True)
    for c in copied_back:
        print(f"  {c.name}  ({c.stat().st_size} bytes)", flush=True)

    # Step 4: parse the SUMMARY.md and print the per-Q tally.
    summary_path = REPO_ROOT / "assessments" / args.slug / "SUMMARY.md"
    tally = parse_marks_from_summary(summary_path)
    if tally is not None:
        print("", flush=True)
        print("Per-question tally (parsed from SUMMARY.md):", flush=True)
        total_awarded = tally.get("total_awarded", "?")
        total_available = tally.get("total_available", "?")
        print(f"  Total: {total_awarded} / {total_available}", flush=True)
        q_keys = sorted([k for k in tally if k.startswith("Q") and not k.endswith("_available") and k[1:].isdigit()], key=lambda x: int(x[1:]))
        for k in q_keys:
            print(f"  {k}: {tally[k]} / {tally.get(k + '_available', '?')}", flush=True)
    else:
        print(f"  (Could not parse SUMMARY.md at {summary_path})", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
