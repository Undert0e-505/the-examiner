"""
src/run.py - top-level orchestrator for the the-examiner auto-pipeline.

This is the script the Telegram trigger drives. It glues together:

  1. Photo staging (Telegram gateway cache -> intake/<slug>/)
  2. OCR pass (src/ocr_batch.py in the codex_lane sandbox)
  3. Markscheme check (papers/<slug>/markscheme.json must exist)
  4. Marking pass (src/mark_batch.py in the codex_lane sandbox)
  5. Publish (src/publish.py -> pages/assessments/<slug>.html,
     bucket self-healing on 404)
  6. Auto-commit + push to origin/main (Pages workflow deploys)
  7. Auto-send the email via Gmail SMTP (src/send_email.py with --send)

Per Aaron's policy on 2026-06-15:
  - Auto-push the marking results (no human commit)
  - Auto-send the email (no human copy-paste)
  - I write the codex prompts from comprehensive templates
  - Active-identity safety rail: active=aaron -> email to Aaron;
    active=will --to live -> email to student + Aaron cc'd.
  - If markscheme.json is missing, abort with email to Aaron
    (subject: "Markscheme missing for <slug>") and exit. Do not
    create the page, do not push, do not email the student.
  - Photos only trigger the run when the trigger message includes
    the /mark <slug> command. Photos alone don't trigger.
  - Photo count verification: if the trigger message doesn't say
    photos=N, the orchestrator blocks and asks on Telegram before
    staging. (In this CLI mode, --photos and --page-order are
    required; the trigger parser in chat handles the human-loop.)

The script is also importable as a module so a future Telegram
trigger layer can call `run_pipeline(slug, photo_paths,
page_order, ...)` directly.

Usage (CLI, from the repo root):

    D:\\Python310\\python.exe src/run.py ^
        --slug aqa-84621h-chemistry-higher-2024-05 ^
        --photos <photo1.jpg> <photo2.jpg> ... ^
        --page-order 11 12 14 15 17 19 20 21 22 23 24 25 26 27 28 29 ^
        --to staging ^
        --yes

    # Dry-run: do every step except the destructive ones (no push,
    # no email send, no git commit). Show what would happen.
    D:\\Python310\\python.exe src/run.py ^
        --slug aqa-84621h-chemistry-higher-2024-05 ^
        --photos ... --page-order ... --to staging --dry-run --yes

    # Markscheme missing -> abort with email to Aaron (no push,
    # no student email, no page).
    D:\\Python310\\python.exe src/run.py --slug <slug-without-markscheme> ...

The --slug, --photos, --page-order, --to flags together are the
"trigger payload." The Telegram trigger parser builds this same
shape from Aaron's /mark <slug> [photos=N] [order=...] message.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Import the lower-level pieces. These are all refactored to be
# importable as modules (run_ocr, run_marking, ensure_kvdb_bucket,
# send_via_gmail, etc.).
import ocr_batch
import mark_batch
import publish
import send_email
from generate_prompts import write_prompt_to_spec_path

REPO_ROOT = publish.REPO_ROOT  # D:/dev/the-examiner
GATEWAY_CACHE = Path("C:/Users/openclaw-agent/.openclaw/media/inbound")
GIT_TOKEN_HELPER_HINT = (
    "GitHub PAT is in Windows Credential Manager. Use `git push` from "
    "the repo root; the credential helper will fetch it."
)


# ---------- Trigger parsing ----------

TRIGGER_RE = re.compile(
    r"^/mark\s+(?P<slug>[a-z0-9-]+)"
    r"(?:\s+photos=(?P<photos>\d+))?"
    r"(?:\s+order=(?P<order>[\d,\s-]+))?"
    r"\s*$",
    re.IGNORECASE,
)


def parse_trigger(message: str) -> dict:
    """Parse a Telegram /mark message into a trigger dict.

    Returns:
        {"slug": str, "photos": int|None, "order": list[int]|None}

    The caller is responsible for blocking on missing photos/order
    and asking the user. parse_trigger is purely a regex-based
    parser — it doesn't talk to Telegram, doesn't ask, doesn't fail
    on missing fields. It returns what it found.

    Examples:
        "/mark aqa-84621h-chemistry-higher-2024-05"
            -> {"slug": "aqa-...", "photos": None, "order": None}
        "/mark aqa-84621h-chemistry-higher-2024-05 photos=11"
            -> {"slug": "aqa-...", "photos": 11, "order": None}
        "/mark aqa-84621h-chemistry-higher-2024-05 photos=11 order=11,12,14,15,17,19,20,21,22,23,24"
            -> {"slug": "aqa-...", "photos": 11, "order": [11, 12, 14, 15, 17, 19, 20, 21, 22, 23, 24]}
    """
    m = TRIGGER_RE.match(message.strip())
    if not m:
        raise ValueError(
            f"Could not parse trigger: {message!r}. Expected: "
            f"'/mark <slug> [photos=N] [order=a,b,c,...]'"
        )
    slug = m.group("slug")
    photos = int(m.group("photos")) if m.group("photos") else None
    order_raw = m.group("order")
    order = None
    if order_raw:
        order = [int(x) for x in re.findall(r"\d+", order_raw)]
    return {"slug": slug, "photos": photos, "order": order}


# ---------- Photo discovery ----------

def discover_photos_for_paper(slug: str) -> list[Path]:
    """Return the photo paths in the gateway cache, in arrival order,
    that look like they belong to this paper.

    This is a placeholder for the real "which photos are for this
    paper" logic. The Telegram trigger layer is expected to hand
    the orchestrator the exact list of photo paths (because the
    gateway doesn't know which photo is for which paper). When
    called from the CLI, --photos is required and authoritative.

    For now, this function returns an empty list, which the
    orchestrator treats as "no photos supplied; abort." The real
    Telegram trigger parser will fill in the photo paths from
    the inbound message metadata before calling run_pipeline.
    """
    return []


# ---------- Markscheme check + abort-email ----------

def check_markscheme_exists(slug: str) -> tuple[bool, str]:
    """Return (exists, path). exists=True means
    papers/<slug>/markscheme.json is in place. exists=False means
    the markscheme hasn't been produced yet, and the orchestrator
    should abort with an email to Aaron.
    """
    path = REPO_ROOT / "papers" / slug / "markscheme.json"
    return (path.is_file(), str(path))


def render_markscheme_missing_email(slug: str, expected_path: str) -> tuple[str, str]:
    """Build the subject and body for the abort-email. The subject
    is a clear, scannable flag; the body has the path that was
    expected and a one-liner saying what to do.
    """
    subject = f"Markscheme missing for {slug}"
    body = (
        f"The orchestrator tried to mark {slug} but the markscheme "
        f"is not in the repo.\n\n"
        f"Expected: {expected_path}\n\n"
        f"Drop the markscheme PDF in the agreed folder "
        f"(D:\\AIProjects\\Aaron\\Jimothy Share\\gcs-papers\\) and run "
        f"`python src/extract_questions.py --slug {slug}` to produce "
        f"the markscheme.json. Then re-trigger /mark {slug}.\n"
    )
    return (subject, body)


# ---------- Git auto-commit + push ----------

def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command. cwd defaults to the repo root. Returns
    the CompletedProcess. Caller is responsible for raising on
    non-zero exit (use check=False for commands that can fail
    gracefully, like `git status`).
    """
    cmd = ["git", *args]
    print(f"  $ {' '.join(cmd)}", flush=True)
    return subprocess.run(
        cmd, cwd=str(cwd or REPO_ROOT), check=check,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def git_has_changes() -> bool:
    """Return True if the working tree has uncommitted changes
    (tracked or untracked) that the orchestrator would commit.
    """
    res = git("status", "--porcelain", check=False)
    return bool(res.stdout.strip())


def auto_commit_and_push(slug: str, total_awarded: int, total_available: int) -> None:
    """Stage the per-run changes (pages/, the bucket file if it
    rotated, the markscheme index), commit, and push to origin/main.

    The commit message is content-derived so the git log tells you
    what was published, not just that publish.py ran. The push is
    the trigger for the Pages deploy workflow.
    """
    # Stage the relevant paths. The orchestrator only stages
    # things the publish step touched, to avoid committing
    # unrelated work-in-progress.
    paths_to_add = [
        f"pages/assessments/{slug}.html",
        f"pages/index.html",
        f"pages/assets/css/styles.css",
        f"pages/assets/js/feedback.js",
        f"papers/{slug}/kvdb-bucket.txt",
    ]
    for p in paths_to_add:
        full = REPO_ROOT / p
        if full.exists():
            git("add", p)
    pct = round(100 * total_awarded / total_available) if total_available else 0
    msg = (
        f"publish: render assessment HTML for {slug} — "
        f"{total_awarded}/{total_available} ({pct}%)"
    )
    git("commit", "-m", msg)
    print(f"  Pushing to origin/main (Pages deploy will follow)...", flush=True)
    git("push", "origin", "main")


# ---------- Pipeline driver ----------

def run_pipeline(
    slug: str,
    photo_paths: list[Path],
    page_order: list[int] | None,
    to_mode: str = "staging",
    *,
    dry_run: bool = False,
    skip_codex: bool = False,
) -> dict:
    """End-to-end pipeline. Returns a dict with the run's artifacts
    and per-step status. The orchestrator's chat trigger calls this
    directly; the CLI also calls it.

    skip_codex: when True, skip the OCR and marking Codex runs.
    Useful for re-running publish + email when the marking output
    is already on disk. (Mostly for the auto-pipeline to recover
    from a push that worked but the email send didn't.)
    """
    summary = {
        "slug": slug,
        "to_mode": to_mode,
        "dry_run": dry_run,
        "stages": {},
    }

    if not skip_codex:
        if not photo_paths:
            raise ValueError("photo_paths must be non-empty when --skip-codex is not set")
        if page_order is not None and len(page_order) != len(photo_paths):
            raise ValueError(
                f"page_order has {len(page_order)} entries but there are "
                f"{len(photo_paths)} photos. They must match."
            )

    # Step 0: markscheme check. If missing, abort with email.
    print("=" * 60, flush=True)
    print(f"Step 0/7: markscheme check for {slug}", flush=True)
    print("=" * 60, flush=True)
    exists, expected_path = check_markscheme_exists(slug)
    if not exists:
        print(f"  markscheme.json not found at {expected_path}", flush=True)
        subject, body = render_markscheme_missing_email(slug, expected_path)
        if dry_run:
            print(f"  [dry-run] Would email Aaron:", flush=True)
            print(f"    Subject: {subject}", flush=True)
            print(f"    Body: {body}", flush=True)
        else:
            send_email.send_via_gmail(
                from_addr=send_email.SMTP_USER,
                to_addrs=[send_email.read_student_json(require_recipient=True)["recipient_email_staging"]],
                cc_addrs=[],
                subject=subject,
                body=body,
                app_password=send_email.get_gmail_app_password(),
            )
            print(f"  emailed Aaron: {subject}", flush=True)
        summary["stages"]["markscheme_check"] = "missing; abort email sent"
        summary["aborted"] = True
        return summary
    print(f"  markscheme.json present at {expected_path}", flush=True)
    summary["stages"]["markscheme_check"] = "ok"

    if not skip_codex:
        # Step 1: stage photos
        print("=" * 60, flush=True)
        print(f"Step 1/7: stage {len(photo_paths)} photos to intake/{slug}/", flush=True)
        print("=" * 60, flush=True)
        if not dry_run:
            ocr = ocr_batch.run_ocr(
                slug=slug,
                job_name=f"ocr-{slug}",
                photo_paths=photo_paths,
                page_order=page_order,
                page_contexts=None,
                batch_id=None,
                yes=True,
                skip_copy_back=False,
            )
            if ocr["codex_returncode"] != 0:
                summary["stages"]["ocr"] = f"codex exit {ocr['codex_returncode']}; abort"
                summary["aborted"] = True
                return summary
            summary["stages"]["ocr"] = "ok"
            summary["transcripts"] = [str(p) for p in (ocr["transcripts_copied_back"] or [])]
        else:
            print(f"  [dry-run] Would stage {len(photo_paths)} photos and call codex_lane", flush=True)
            summary["stages"]["ocr"] = "dry-run"

        # Step 2: marking pass
        print("=" * 60, flush=True)
        print(f"Step 2/7: marking pass for {slug}", flush=True)
        print("=" * 60, flush=True)
        if not dry_run:
            mark = mark_batch.run_marking(
                slug=slug,
                job_name=f"mark-{slug}",
                yes=True,
                skip_copy_back=False,
            )
            if mark["codex_returncode"] != 0:
                summary["stages"]["marking"] = f"codex exit {mark['codex_returncode']}; abort"
                summary["aborted"] = True
                return summary
            summary["stages"]["marking"] = "ok"
            summary["marking_files"] = [str(p) for p in (mark["marking_files_copied_back"] or [])]
            summary["tally"] = mark.get("tally")
        else:
            print(f"  [dry-run] Would call codex_lane for marking", flush=True)
            summary["stages"]["marking"] = "dry-run"
    else:
        summary["stages"]["ocr"] = "skipped"
        summary["stages"]["marking"] = "skipped"

    # Step 3: publish
    print("=" * 60, flush=True)
    print(f"Step 3/7: publish (render pages/assessments/{slug}.html)", flush=True)
    print("=" * 60, flush=True)
    if not dry_run:
        student = publish.read_student_json(require_recipient=False)
        meta = publish.publish_one(slug, student, dry_run=False)
        publish.publish_index([meta], dry_run=False)
        publish.copy_assets(dry_run=False)
        summary["stages"]["publish"] = "ok"
        summary["meta"] = {
            "total_awarded": meta.get("total_awarded"),
            "total_available": meta.get("total_available"),
            "kvdb_bucket": meta.get("kvdb_bucket"),
            "bucket_rotated": meta.get("bucket_rotated"),
        }
    else:
        print(f"  [dry-run] Would render pages/assessments/{slug}.html", flush=True)
        summary["stages"]["publish"] = "dry-run"
        # For dry-run, peek at the marked files to estimate totals.
        from publish import parse_summary, parse_question_marking
        try:
            s = parse_summary(slug)
            summary["meta"] = {
                "total_awarded": s.get("total_awarded"),
                "total_available": s.get("total_available"),
            }
        except Exception:
            summary["meta"] = {}

    # Step 4: auto-commit + push
    print("=" * 60, flush=True)
    print("Step 4/7: git auto-commit + push to origin/main", flush=True)
    print("=" * 60, flush=True)
    if not dry_run:
        if not git_has_changes():
            print("  no changes to commit; skipping push", flush=True)
            summary["stages"]["git"] = "no-op"
        else:
            ta = summary.get("meta", {}).get("total_awarded", 0)
            tp = summary.get("meta", {}).get("total_available", 0)
            try:
                auto_commit_and_push(slug, ta, tp)
                summary["stages"]["git"] = "ok"
            except subprocess.CalledProcessError as e:
                print(f"  git push failed: {e}", flush=True)
                summary["stages"]["git"] = f"failed: {e}"
                summary["aborted"] = True
                return summary
    else:
        print("  [dry-run] Would git add + commit + push to origin/main", flush=True)
        summary["stages"]["git"] = "dry-run"

    # Step 5: wait for the Pages workflow to deploy (so the public
    # URL is live when the email lands).
    print("=" * 60, flush=True)
    print("Step 5/7: wait for Pages deploy", flush=True)
    print("=" * 60, flush=True)
    if not dry_run:
        if wait_for_pages_deploy(slug, timeout_sec=120):
            summary["stages"]["pages_deploy"] = "ok"
        else:
            print("  Pages deploy not seen within 120s; sending email anyway", flush=True)
            summary["stages"]["pages_deploy"] = "timeout"
    else:
        print("  [dry-run] Would poll the latest workflow run for completion", flush=True)
        summary["stages"]["pages_deploy"] = "dry-run"

    # Step 6: send the email
    print("=" * 60, flush=True)
    print(f"Step 6/7: send email ({to_mode})", flush=True)
    print("=" * 60, flush=True)
    if not dry_run:
        student = send_email.read_student_json(require_recipient=True)
        summary_parse = publish.parse_summary(slug)
        questions = []
        for q_row in summary_parse.get("q_rows", []):
            q_short = q_row["q"]
            q_padded = "Q" + q_short.lstrip("Q").zfill(2)
            q = publish.parse_question_marking(slug, q_padded)
            if q is not None:
                questions.append(q)
        questions.sort(key=lambda q: int(q["qnum"]))
        public_url = f"https://undert0e-505.github.io/the-examiner/assessments/{slug}.html"
        subj = send_email.subject_for(slug, summary_parse)
        body = send_email.render_email_body(slug, summary_parse, questions, student, public_url, to_mode)
        to_primary, to_list, cc_list = send_email.resolve_recipients(student, to_mode)
        try:
            send_email.send_via_gmail(
                from_addr=send_email.SMTP_USER,
                to_addrs=to_list,
                cc_addrs=cc_list,
                subject=subj,
                body=body,
                app_password=send_email.get_gmail_app_password(),
            )
            summary["stages"]["email"] = f"sent to {', '.join(to_list)}" + (f" (cc: {', '.join(cc_list)})" if cc_list else "")
        except Exception as e:
            print(f"  email send failed: {e}", flush=True)
            summary["stages"]["email"] = f"failed: {e}"
            summary["aborted"] = True
            return summary
    else:
        print(f"  [dry-run] Would send email to {to_mode} recipient via Gmail SMTP", flush=True)
        summary["stages"]["email"] = "dry-run"

    return summary


def wait_for_pages_deploy(slug: str, timeout_sec: int = 120) -> bool:
    """Poll the latest Pages workflow run and wait for it to complete
    successfully. Returns True if seen, False if timed out.

    Uses the gh API to read the workflow runs. Auth is via the
    PAT in Windows Credential Manager (per MEMORY.md).
    """
    import base64
    import urllib.request
    # Read the PAT from credential manager (same store as git).
    # The gh CLI's auth comes from `gh auth login`, but for raw
    # REST calls we can use a header constructed from a username
    # + PAT (the username is ignored for fine-grained PATs).
    from send_email import _read_credential
    token = _read_credential("github:pat") or _read_credential("github-token")
    if not token:
        print("  No GitHub PAT in Credential Manager; skipping deploy wait", flush=True)
        return False
    auth = "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    headers = {
        "Authorization": auth,
        "Accept": "application/vnd.github+json",
        "User-Agent": "JimothyScript",
    }
    deadline = time.time() + timeout_sec
    last_run_id = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/Undert0e-505/the-examiner/actions/runs?per_page=1",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                runs = json.loads(resp.read())
            if not runs.get("workflow_runs"):
                time.sleep(5)
                continue
            run = runs["workflow_runs"][0]
            last_run_id = run["id"]
            status = run["status"]
            conclusion = run["conclusion"]
            print(f"  Pages workflow {last_run_id}: status={status} conclusion={conclusion}", flush=True)
            if status == "completed":
                return conclusion == "success"
        except Exception as e:
            print(f"  GitHub API error: {e}", flush=True)
        time.sleep(10)
    return False


# ---------- CLI ----------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Top-level orchestrator: photos -> OCR -> mark -> publish -> "
            "push -> email. Designed to be driven by the Telegram trigger "
            "in chat, but also runnable from the CLI for testing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--slug", required=True, help="Paper slug.")
    p.add_argument(
        "--photos", nargs="*", type=Path, default=[],
        help="Photo paths in glob order. Required unless --skip-codex is set.",
    )
    p.add_argument(
        "--page-order", type=int, nargs="*", default=None,
        help="Printed page number for each photo, in --photos order. Gaps are fine.",
    )
    p.add_argument("--to", choices=("staging", "live"), default="staging",
                   help="Which recipient to email. Default staging (Aaron).")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    p.add_argument("--dry-run", action="store_true",
                   help="Do every step except the destructive ones (no push, no email send, no git commit).")
    p.add_argument("--skip-codex", action="store_true",
                   help="Skip OCR and marking. Useful for re-running publish + email when marking is already done.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if not args.skip_codex and not args.photos:
        print("ERROR: --photos is required unless --skip-codex is set.", file=sys.stderr)
        return 1

    if not args.yes and not args.dry_run:
        print(f"This will run the full pipeline for {args.slug}:", flush=True)
        print(f"  photos: {len(args.photos)}")
        if args.page_order:
            print(f"  page order: {args.page_order}")
        print(f"  to: {args.to}")
        print(f"  Includes: codex OCR, codex marking, publish, git push, SMTP email send.")
        resp = input("Continue? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    result = run_pipeline(
        slug=args.slug,
        photo_paths=args.photos,
        page_order=args.page_order,
        to_mode=args.to,
        dry_run=args.dry_run,
        skip_codex=args.skip_codex,
    )

    print("", flush=True)
    print("=" * 60, flush=True)
    print("PIPELINE SUMMARY", flush=True)
    print("=" * 60, flush=True)
    for stage, status in result.get("stages", {}).items():
        print(f"  {stage}: {status}", flush=True)
    if result.get("aborted"):
        print(f"  ABORTED: {result.get('aborted')}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
