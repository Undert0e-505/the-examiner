"""
src/run.py - top-level orchestrator for the the-examiner auto-pipeline.

This is the script the Telegram trigger drives. It glues together:

  0. Papers sync (Drive-mirrored exam-papers/ -> papers/,
     index_papers.py, extract_questions.py for any missing slug)
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
    active=student --to live -> email to student + Aaron cc'd.
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

# Pipeline instrumentation: every step in run_pipeline() calls
# _log_step_start("N/total", "name") at the top and
# _log_step_done("N/total", "name", t0) at the bottom. The
# _PIPELINE_T0 is the run-wide start time, also printed on entry.
# This makes the log file self-describing: if a run stalls, the
# last "step_start" with no matching "step_done" is the one
# that's stuck, and the time since tells you how long.
_PIPELINE_T0: float | None = None

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

def _log_step_start(step: str, name: str) -> None:
    print(f"[{_now_iso()}] step_start  {step}  {name}", flush=True)

def _log_step_done(step: str, name: str, t0: float) -> None:
    print(f"[{_now_iso()}] step_done   {step}  {name}  ({time.time()-t0:.1f}s)", flush=True)

# Import the lower-level pieces. These are all refactored to be
# importable as modules (run_ocr, run_marking, ensure_kvdb_bucket,
# send_via_gmail, etc.).
import ocr_batch
import mark_batch
import papers_sync
import publish
import send_email
from generate_prompts import write_prompt_to_spec_path
from backends import ollama_vision

REPO_ROOT = publish.REPO_ROOT  # D:/dev/the-examiner
GATEWAY_CACHE = Path("C:/Users/openclaw-agent/.openclaw/media/inbound")
GIT_TOKEN_HELPER_HINT = (
    "GitHub PAT is in Windows Credential Manager. Use `git push` from "
    "the repo root; the credential helper will fetch it."
)


# ---------- Trigger parsing ----------

# Regex: /mark <slug> [photos=N] [order=...]
# OR:    /mark [N] [pages|photos|pictures]
# OR:    /mark
# When the slug and order are absent (auto-discover mode), the
# orchestrator picks the N most recent photos in the gateway cache
# and runs the discovery pass.
TRIGGER_RE = re.compile(
    r"^/mark"
    r"(?:\s+(?P<slug>[a-z0-9-]+))?"
    r"(?:\s+(?P<count>\d+))?"
    r"(?:\s+(?P<noun>pages|photos|pictures))?"
    r"(?:\s+photos=(?P<photos>\d+))?"
    r"(?:\s+order=(?P<order>[\d,\s-]+))?"
    r"\s*$",
    re.IGNORECASE,
)


def parse_trigger(message: str) -> dict:
    """Parse a Telegram /mark message into a trigger dict.

    Returns:
        {
          "slug": str | None,        # canonical slug, or None for auto-discover
          "photos": int | None,      # explicit photo count, or None
          "order": list[int] | None, # explicit page order, or None
          "auto_discover": bool,     # True if discovery pass is needed
          "count": int | None,       # N from "/mark N pages", or None
        }

    parse_trigger is purely a regex-based parser Ã¢â‚¬â€ it doesn't talk
    to Telegram, doesn't ask, doesn't fail on missing fields. It
    returns what it found. The caller is responsible for blocking
    on missing required fields and asking the user.

    Examples:
        "/mark aqa-84621h-chemistry-higher-2024-05 photos=10 order=2,3,4"
            -> {"slug": "aqa-...", "photos": 10, "order": [2,3,4],
                "auto_discover": False, "count": None}
        "/mark 26 pages"
            -> {"slug": None, "photos": None, "order": None,
                "auto_discover": True, "count": 26}
        "/mark"
            -> {"slug": None, "photos": None, "order": None,
                "auto_discover": True, "count": None}
        "/mark 26 photos photos=26"
            -> {"slug": None, "photos": 26, "order": None,
                "auto_discover": True, "count": 26}
                # the explicit photos=26 wins; the "26 photos" form
                # is treated as a count hint that matches.
    """
    m = TRIGGER_RE.match(message.strip())
    if not m:
        raise ValueError(
            f"Could not parse trigger: {message!r}. Expected one of: "
            f"'/mark <slug> [photos=N] [order=a,b,c,...]' "
            f"or '/mark [N] [pages|photos|pictures]' "
            f"or '/mark' (auto-discover the latest batch)."
        )
    slug = m.group("slug")
    count_raw = m.group("count")
    noun = (m.group("noun") or "").lower()
    photos_raw = m.group("photos")
    order_raw = m.group("order")
    count = int(count_raw) if count_raw else None
    photos = int(photos_raw) if photos_raw else None
    order = None
    if order_raw:
        order = [int(x) for x in re.findall(r"\d+", order_raw)]

    # If the slug is purely numeric, treat it as a count, not a
    # slug. (Otherwise "/mark 26 pages" parses as slug="26", which
    # is wrong: the user said "26 pages", not "the slug 26".)
    if slug is not None and slug.isdigit():
        if count is None:
            count = int(slug)
        slug = None

    # Decision: auto-discover iff no slug and no explicit order.
    # If the user gave photos=N without a slug, that's still
    # auto-discover (we discover the slug, and N is a hint for
    # how many photos to pull from the cache).
    auto_discover = (slug is None) and (order is None)

    # If the user said "N pages" and ALSO said "photos=M" where
    # M != N, that's a conflict Ã¢â‚¬â€ caller should warn but we'll
    # trust photos=M as the explicit override.
    return {
        "slug": slug,
        "photos": photos,
        "order": order,
        "auto_discover": auto_discover,
        "count": count,
        "noun": noun,
    }


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


# ---------- Auto-discover (paper + page order) ----------

def pick_latest_photos(count: int | None, cache_dir: Path) -> list[Path]:
    """Return the most recent photos in the gateway cache, in
    receipt order (oldest-first by LastWriteTime). If count is
    None, returns all photos currently in the cache.

    This is the photo-source for auto-discover mode. The
    assumption is that the user just sent a Telegram batch and
    these are the new photos; the older photos in the cache are
    from previous runs of the same paper and are already in
    intake/<slug>/.

    Errors if `count` is given and the cache has fewer than that
    many photos. For the Telegram trigger, use `wait_for_photos`
    instead -- it polls for the count to arrive and falls back
    to "all current photos" if the user-specified count is
    never reached.
    """
    if not cache_dir.is_dir():
        raise FileNotFoundError(f"gateway cache not found: {cache_dir}")
    all_photos = [
        p for p in cache_dir.glob("*.jpg") if p.is_file()
    ]
    if not all_photos:
        raise FileNotFoundError(f"no photos in gateway cache: {cache_dir}")
    all_photos.sort(key=lambda p: p.stat().st_mtime)  # oldest-first
    if count is not None:
        if count > len(all_photos):
            raise FileNotFoundError(
                f"expected {count} photos in cache, found {len(all_photos)}"
            )
        return all_photos[-count:]
    return all_photos


LAST_BATCH_MARKER = REPO_ROOT / "intake" / ".last_batch_marker"


def _read_last_batch_marker() -> float | None:
    """Return the mtime of the last-batch marker file, or None
    if the marker doesn't exist (first-ever run, or marker
    was wiped). The marker is updated by mark_batch_started()
    at the beginning of every orchestrator run.
    """
    if not LAST_BATCH_MARKER.is_file():
        return None
    return LAST_BATCH_MARKER.stat().st_mtime


def mark_batch_started() -> None:
    """Touch the last-batch marker file. Called at the start of
    every orchestrator run so that the NEXT bare-/mark can find
    photos that arrived after this one.
    """
    LAST_BATCH_MARKER.parent.mkdir(parents=True, exist_ok=True)
    LAST_BATCH_MARKER.touch()


def wait_for_photos(
    count: int | None,
    cache_dir: Path,
    *,
    timeout_sec: int = 90,
    poll_interval_sec: int = 5,
    stability_sec: int = 15,
    recent_window_sec: int = 1800,  # 30 min - long enough for the user to walk away after sending
) -> list[Path]:
    """Wait for photos to land in the gateway cache.

    The Telegram gateway delivers photos one at a time; it can
    take 30-60 seconds for a 26-photo batch to all appear in the
    cache. If the user said "/mark 26 pages" and we start the
    orchestrator the moment the message arrives, we'll see fewer
    than 26 photos initially. The fix is to wait, not to error
    out and ask the user.

    Behaviour:
      - If `count` is given: poll every `poll_interval_sec` for
        up to `timeout_sec`. Return the latest `count` photos
        once the cache has at least that many. If the timeout
        fires first, return whatever is there (the orchestrator
        will discover what it can and warn the user that the
        count was short).
      - If `count` is None (bare /mark): use a "stability"
        check (no new photos for `stability_sec` seconds). When
        the cache settles, return only the photos that arrived
        AFTER the last orchestrator run (i.e. photos with
        mtime > LAST_BATCH_MARKER's mtime). If the marker
        doesn't exist (first-ever run), fall back to the
        `recent_window_sec` heuristic: photos modified in the
        last 5 minutes. The orchestrator's discovery pass
        figures out the paper from those.
      - If no photos at all within the timeout, raise
        FileNotFoundError so the orchestrator aborts with a
        clear "I never got any photos" message.

    The default count-timeout (90s) is enough for typical
    26-photo Telegram batches. For larger batches (50+ photos),
    bump it. The stability check default (15s) is tuned to
    Telegram's delivery cadence: typical photo-to-photo gap is
    <1s, so 15s of silence reliably means the batch is done.
    """
    if not cache_dir.is_dir():
        raise FileNotFoundError(f"gateway cache not found: {cache_dir}")

    def _list_all():
        return sorted(
            [p for p in cache_dir.glob("*.jpg") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
        )

    def _list_after(cutoff_mtime: float):
        return sorted(
            [p for p in cache_dir.glob("*.jpg") if p.is_file() and p.stat().st_mtime > cutoff_mtime],
            key=lambda p: p.stat().st_mtime,
        )

    if count is None:
        # Stability check: wait for `stability_sec` of no new photos.
        deadline = time.time() + timeout_sec
        last_n = -1
        last_change_at = time.time()
        while time.time() < deadline:
            n = len(_list_all())
            if n != last_n:
                print(f"  wait_for_photos: cache has {n} photos (stability check, need {stability_sec}s of silence)", flush=True)
                last_n = n
                last_change_at = time.time()
            elif time.time() - last_change_at >= stability_sec:
                # Settled. Filter to photos that arrived after the
                # last orchestrator run.
                all_photos = _list_all()
                if not all_photos:
                    raise FileNotFoundError(
                        f"no photos in gateway cache after {timeout_sec}s stability check"
                    )
                marker_mtime = _read_last_batch_marker()
                if marker_mtime is not None:
                    batch_photos = _list_after(marker_mtime)
                    print(
                        f"  wait_for_photos: settled, returning {len(batch_photos)} photos "
                        f"after last-batch marker (cache has {len(all_photos)} total)",
                        flush=True,
                    )
                else:
                    # No marker yet (first run). Use the recent
                    # window as a heuristic.
                    cutoff = time.time() - recent_window_sec
                    batch_photos = _list_after(cutoff)
                    print(
                        f"  wait_for_photos: no last-batch marker; "
                        f"returning {len(batch_photos)} photos modified in the last "
                        f"{recent_window_sec}s (cache has {len(all_photos)} total)",
                        flush=True,
                    )
                if not batch_photos:
                    raise FileNotFoundError(
                        f"cache settled at {n} photos but none are newer than the "
                        f"last orchestrator run. Send the photos and try again."
                    )
                return batch_photos
            time.sleep(poll_interval_sec)
        # Timed out without settling. Filter to recent photos.
        all_photos = _list_all()
        if not all_photos:
            raise FileNotFoundError(
                f"timed out after {timeout_sec}s with no photos in gateway cache"
            )
        marker_mtime = _read_last_batch_marker()
        if marker_mtime is not None:
            batch_photos = _list_after(marker_mtime)
        else:
            cutoff = time.time() - recent_window_sec
            batch_photos = _list_after(cutoff)
        print(
            f"  wait_for_photos: timed out after {timeout_sec}s, "
            f"returning {len(batch_photos)} photos (stability check incomplete)",
            flush=True,
        )
        if not batch_photos:
            raise FileNotFoundError(
                f"timed out after {timeout_sec}s; no recent photos in gateway cache"
            )
        return batch_photos

    # Count-given path: wait until at least N photos have arrived
    # AFTER the last-batch marker (or after this function was called
    # if no marker exists), then return those N. This prevents picking
    # up stale photos from previous runs when the cache hasn't been
    # cleared.
    start_mtime = time.time()
    marker_mtime = _read_last_batch_marker()
    cutoff = max(marker_mtime or 0, start_mtime - recent_window_sec)
    deadline = start_mtime + timeout_sec
    last_n = -1
    while time.time() < deadline:
        batch_photos = _list_after(cutoff)
        n = len(batch_photos)
        if n != last_n:
            total = len(_list_all())
            print(f"  wait_for_photos: {n} new photos since /mark (cache has {total} total, waiting for {count})", flush=True)
            last_n = n
        if n >= count:
            return batch_photos[-count:]
        time.sleep(poll_interval_sec)
    # Timed out. Return what we have.
    batch_photos = _list_after(cutoff)
    if not batch_photos:
        raise FileNotFoundError(
            f"timed out after {timeout_sec}s waiting for {count} new photos; "
            f"got 0. The Telegram batch may not have arrived."
        )
    print(
        f"  wait_for_photos: timed out after {timeout_sec}s, "
        f"got {len(batch_photos)}/{count} new photos",
        flush=True,
    )
    return batch_photos


def auto_discover(
    slug: str | None,
    photos_hint: int | None,
    engine: str = "ollama",
) -> dict:
    """Run the discovery pass: wait for the latest N photos to
    land in the cache, run the chosen engine (Codex default, or
    Ollama opt-in) to identify the paper + page order, rename
    the photos in the real repo's intake/<slug>/, and return the
    discovered slug + page_order.

    `photos_hint` is the user-supplied count (e.g. 26 from
    "/mark 26 pages"). We use it as a WAIT TARGET, not a hard
    requirement: we poll the gateway cache until at least that
    many photos have arrived, OR a 90s timeout fires (whichever
    is first). If the timeout fires with fewer photos, we
    proceed with what we have and warn the user.

    This is the fix for the 2026-06-15 chat where Aaron sent
    "/mark 26 pages" while the photos were still uploading.
    Previously, the orchestrator saw only 10 of 26 photos
    initially and asked the user for a slug (which it should
    have been able to discover from the photos). Now it waits.

    Returns a dict with the same shape as the run_pipeline result
    field for a discovered batch:
        {
          "slug": str,
          "page_order": list[int],
          "cover_paper_code": str,
          "cover_text": str,
          "confidence": str,
        }

    Raises ValueError if the discovery result doesn't match a
    known paper, or if no photos are available.
    """
    import discover_batch as db
    cache_dir = GATEWAY_CACHE
    photo_paths = wait_for_photos(photos_hint, cache_dir)
    # Mark the batch as started ONLY after we have a non-empty
    # photo set. Otherwise a failed wait poisons the marker for
    # the next run (the marker would be "now", and all photos
    # already in the cache would be filtered as old). The marker
    # is for "find photos that arrived after this run started";
    # if the run didn't start successfully, no marker should
    # be written.
    if not photo_paths:
        raise FileNotFoundError(
            f"auto_discover: no photos in {cache_dir}. "
            f"Send the photos and try again."
        )
    mark_batch_started()
    print(f"Auto-discover: picked {len(photo_paths)} photos from {cache_dir}", flush=True)
    if photos_hint is not None and len(photo_paths) < photos_hint:
        print(
            f"  NOTE: hint was {photos_hint} photos, got {len(photo_paths)}. "
            f"Proceeding with what arrived; the discovery pass will see a "
            f"shorter stack than expected.",
            flush=True,
        )

    job_name = f"discover-{int(time.time())}"
    result = db.discover_batch(
        photo_paths=photo_paths,
        job_name=job_name,
        yes=True,
        engine=engine,
    )
    discovered_slug = result["slug"]
    if slug is not None and slug != discovered_slug:
        print(
            f"WARN: trigger said slug={slug!r} but discovery found "
            f"slug={discovered_slug!r}; trusting discovery.",
            flush=True,
        )

    # Clear any pre-existing photos in the destination before
    # restaging. Auto-discover is a redo from scratch; the safety
    # rail in restage_real_repo_after_discovery would otherwise
    # abort with FileExistsError. The discovered_slug is the
    # orchestrator's source of truth (Codex read the cover), not
    # any pre-existing filename.
    dest_dir = REPO_ROOT / "intake" / discovered_slug
    if dest_dir.exists():
        existing_jpgs = sorted(dest_dir.glob("*.jpg"))
        if existing_jpgs:
            for old in existing_jpgs:
                old.unlink()
            print(
                f"  Cleared {len(existing_jpgs)} pre-existing .jpg from "
                f"intake/{discovered_slug}/ before restage",
                flush=True,
            )

    # Rename the staged photos from intake/_discover/<job>/ to
    # intake/<slug>/<page>.jpg in the REAL repo. The OCR pass
    # will then run with --skip-staging=True.
    new_paths = db.restage_real_repo_after_discovery(
        job_name=job_name,
        slug=discovered_slug,
        page_numbers_by_index=result["page_numbers"],
    )
    print(f"Restaged {len(new_paths)} photos into intake/{discovered_slug}/", flush=True)

    # Cleanup the temporary discovery intake.
    db.cleanup_discovery_intake(job_name)

    return {
        "slug": discovered_slug,
        "page_order": result["page_order"],
        "cover_paper_code": result["cover_paper_code"],
        "cover_text": result["cover_text"],
        "confidence": result["confidence"],
        "photo_count": len(new_paths),
        "page_numbers": result["page_numbers"],
    }


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
        f"(D:\\AIProjects\\Aaron\\Jimothy Share\\exam-papers\\) and run "
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
    _cmd_str = ' '.join(cmd)
    try:
        print(f"  $ {_cmd_str}", flush=True)
    except UnicodeEncodeError:
        print(f"  $ {_cmd_str.encode('ascii', 'replace').decode('ascii')}", flush=True)
    return subprocess.run(
        cmd, cwd=str(cwd or REPO_ROOT), check=check,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
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

    Push safety rail (2026-06-16): we don't assume the worktree
    is checked out on `main`. The orchestrator has run on
    `m3-adapter` and on feature branches in the past, and a
    plain `git push origin main` is a silent no-op in that
    case (local `main` ref doesn't move Ã¢â€ â€™ "Everything up-to-
    date" with no error). The bug bit us on 2026-06-16: a
    fresh 69/100 publish landed on the worktree's current
    branch but never reached origin/main, so GitHub Pages
    kept serving the old page for ~90 minutes.

    Fix: push HEAD of whatever branch we're on to the
    `main` ref on the remote explicitly. Then verify
    origin/main actually moved to the new HEAD. If it
    didn't, abort loudly with a non-zero exit instead of
    sending the email and pretending everything's fine.
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
        f"publish: render assessment HTML for {slug} Ã¢â‚¬â€ "
        f"{total_awarded}/{total_available} ({pct}%)"
    )
    # Capture local HEAD before commit so we can verify
    # origin/main moved to it after the push.
    local_head_before = git("rev-parse", "HEAD", check=False).stdout.strip()
    git("commit", "-m", msg)
    local_head_after = git("rev-parse", "HEAD", check=False).stdout.strip()
    if local_head_after == local_head_before:
        # Nothing was committed (paths_to_add had no changes).
        # No push needed, no Pages deploy, no email. Caller
        # already checked `git_has_changes()` so this should
        # be rare, but be explicit anyway.
        print(
            f"  no new commit (HEAD unchanged at {local_head_after[:8]}); "
            f"skipping push",
            flush=True,
        )
        return
    print(
        f"  Pushing local HEAD {local_head_after[:8]} to origin/main "
        f"(Pages deploy will follow)...",
        flush=True,
    )
    # Push the current branch's HEAD to the remote `main` ref
    # explicitly, regardless of what the local branch is
    # called. This works whether we're on main, m3-adapter,
    # or a feature branch.
    push = git("push", "origin", f"{local_head_after}:refs/heads/main", check=False)
    if push.returncode != 0:
        # The push command itself failed (network, auth,
        # non-fast-forward). Don't pretend it worked.
        raise RuntimeError(
            f"git push to origin/main failed (exit {push.returncode}):\n"
            f"{push.stdout}"
        )
    # Post-push verification: confirm origin/main actually
    # moved to the new HEAD. If it didn't, the push looked
    # like it succeeded but didn't update the remote ref.
    remote_head = git("rev-parse", "origin/main", check=False).stdout.strip()
    if remote_head != local_head_after:
        raise RuntimeError(
            f"PUSH APPEARED TO SUCCEED BUT origin/main DID NOT MOVE.\n"
            f"  local HEAD:  {local_head_after}\n"
            f"  origin/main: {remote_head}\n"
            f"  This is the silent-failure class from 2026-06-16. "
            f"Manual intervention required."
        )
    print(
        f"  origin/main confirmed at {remote_head[:8]}",
        flush=True,
    )


# ---------- Pipeline driver ----------

def run_pipeline(
    slug: str,
    photo_paths: list[Path],
    page_order: list[int] | None,
    to_mode: str = "staging",
    *,
    dry_run: bool = False,
    skip_codex: bool = False,
    auto_discover_mode: bool = False,
    photos_hint: int | None = None,
    engine: str = "ollama",
) -> dict:
    """End-to-end pipeline. Returns a dict with the run's artifacts
    and per-step status. The orchestrator's chat trigger calls this
    directly; the CLI also calls it.

    skip_codex: when True, skip the OCR and marking Codex runs.
    Useful for re-running publish + email when the marking output
    is already on disk. (Mostly for the auto-pipeline to recover
    from a push that worked but the email send didn't.)

    auto_discover_mode: when True, the orchestrator runs a Codex
    discovery pass to identify the paper slug + printed page
    order from the photos themselves, instead of trusting a
    pre-supplied slug + page_order. The slug and page_order
    arguments are still optional; if slug is given it's used as
    a hint and the discovery result is trusted if they conflict.

    photos_hint: when auto_discover_mode is True, this is the
    expected photo count. If None, the orchestrator picks all
    photos currently in the cache (oldest-first by LastWriteTime).
    """
    summary = {
        "slug": slug,
        "to_mode": to_mode,
        "dry_run": dry_run,
        "stages": {},
    }

    # Step 0 (always): sync PDFs from the Drive-mirrored exam-papers
    # folder into papers/, run the indexer, and ensure every
    # papers/<slug>/ has paper.json + markscheme.json. This is the
    # "drop PDFs in Drive and walk away" hook -- the user doesn't
    # need to run index_papers.py or extract_questions.py by hand.
    # Idempotent and skipped in dry-run mode (it would still work,
    # but the LLM call would block). When skip_codex is set we also
    # skip -- the user is re-running publish/email only and doesn't
    # want a multi-minute extract in the way.
    if not skip_codex and not dry_run:
        _log_step_start("0/8", "papers_sync")
        t0 = time.time()
        sync_summary = papers_sync.ensure_papers_indexed(dry_run=False)
        summary["stages"]["papers_sync"] = "ok" if sync_summary else "failed"
        summary["papers_sync"] = sync_summary
        if not sync_summary or sync_summary.get("indexed") is False and sync_summary.get("synced_pdfs"):
            # Only abort if we tried to index and it failed. A
            # successful sync with no new PDFs is fine.
            pass
        _log_step_done("0/8", "papers_sync", t0)
    else:
        if skip_codex:
            print("Step 0/8: papers_sync skipped (--skip-codex)", flush=True)
        else:
            print("Step 0/8: papers_sync skipped (--dry-run)", flush=True)
        summary["stages"]["papers_sync"] = "skipped"

    # Step 0 (auto-discover only): identify the paper and the
    # printed page order from the photos themselves. This runs
    # before the markscheme check because we need to know which
    # markscheme to look up.
    if auto_discover_mode:
        print("=" * 60, flush=True)
        print("Step 1/8: auto-discover (paper + page order)", flush=True)
        print("=" * 60, flush=True)
        _log_step_start("1/8", "auto-discover")
        t0 = time.time()
        if dry_run:
            print("  [dry-run] Would run discovery pass; skipping.", flush=True)
            summary["stages"]["auto_discover"] = "dry-run"
        else:
            try:
                discovery = auto_discover(slug, photos_hint, engine=engine)
            except ValueError as e:
                # Paper not in repo, or photos hint too small, etc.
                print(f"  discovery failed: {e}", flush=True)
                summary["stages"]["auto_discover"] = f"failed: {e}"
                summary["aborted"] = True
                return summary
            slug = discovery["slug"]
            # After auto_discover, the photos are correctly named
            # at intake/<slug>/<page>.jpg in the real repo. The
            # OCR pass will use --skip-staging=True to read them
            # from there.
            # Build page_order parallel to photo_paths (one entry
            # per photo). The cover photo (file_index 1) is always
            # treated as page 1; other photos use the discovered
            # page number. For photos with unknown page numbers,
            # the orchestrator aborts and asks the user to fix the
            # intake folder by hand.
            page_numbers = discovery["page_numbers"]
            intake_dir = REPO_ROOT / "intake" / slug
            photo_paths = sorted(intake_dir.glob("*.jpg"))
            page_order = []
            unknown_indices = []
            for idx in sorted(page_numbers.keys()):
                p = page_numbers.get(idx)
                if p is None and idx == 1:
                    p = 1  # Cover defaults to page 1
                if p is None:
                    unknown_indices.append(idx)
                page_order.append(p)
            while len(page_order) < len(photo_paths):
                page_order.append(None)
                unknown_indices.append(len(page_order))
            if unknown_indices:
                raise ValueError(
                    f"Discovery could not determine the printed page number "
                    f"for {len(unknown_indices)} photo(s) at file index(es) "
                    f"{unknown_indices}. The orchestrator refuses to guess; "
                    f"fix the intake folder by hand (rename "
                    f"intake/{slug}/unknown-NN.jpg to NN.jpg where NN is "
                    f"the printed page number) and re-trigger /mark."
                )
            print(f"  discovered slug={slug}, {len(photo_paths)} photos, "
                  f"page_order={page_order}", flush=True)
            summary["stages"]["auto_discover"] = "ok"
            summary["discovered"] = {
                "slug": slug,
                "cover_paper_code": discovery["cover_paper_code"],
                "cover_text": discovery["cover_text"],
                "confidence": discovery["confidence"],
            }
            _log_step_done("1/8", "auto-discover", t0)

    if not skip_codex:
        if not photo_paths:
            raise ValueError("photo_paths must be non-empty when --skip-codex is not set")
        if page_order is not None and len(page_order) != len(photo_paths):
            raise ValueError(
                f"page_order has {len(page_order)} entries but there are "
                f"{len(photo_paths)} photos. They must match."
            )

    # Step 1: markscheme check. If missing, abort with email.
    print("=" * 60, flush=True)
    print(f"Step 2/8: markscheme check for {slug}", flush=True)
    print("=" * 60, flush=True)
    _log_step_start("2/8", "markscheme_check")
    t0 = time.time()
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
        _log_step_done("2/8", "markscheme_check (missing; abort)", t0)
        return summary
    print(f"  markscheme.json present at {expected_path}", flush=True)
    summary["stages"]["markscheme_check"] = "ok"
    _log_step_done("2/8", "markscheme_check", t0)

    if not skip_codex:
        # Step 2: stage photos
        print("=" * 60, flush=True)
        print(f"Step 3/8: stage {len(photo_paths)} photos to intake/{slug}/", flush=True)
        print("=" * 60, flush=True)
        _log_step_start("3/8", "stage+ocr")
        t0 = time.time()
        if not dry_run:
            if engine == "ollama":
                # Ollama vision path - no Codex
                page_numbers = page_order if page_order is not None else list(range(1, len(photo_paths) + 1))
                ocr = ollama_vision.run_ocr(
                    slug=slug,
                    page_numbers=page_numbers,
                    skip_staging=auto_discover_mode,
                )
            else:
                ocr = ocr_batch.run_ocr(
                    slug=slug,
                    job_name=f"ocr-{slug}",
                    photo_paths=photo_paths,
                    page_order=page_order,
                    page_contexts=None,
                    batch_id=None,
                    yes=True,
                    skip_copy_back=False,
                    skip_staging=auto_discover_mode,
                )
            if ocr["codex_returncode"] != 0:
                err_tail = ocr.get("codex_err_tail", "").strip()
                # Surface the actual reason in the log. If the err log
                # is empty, fall back to the raw exit code; the operator
                # can dig into the sandbox .codex_run/ dir for more.
                if err_tail:
                    summary["stages"]["ocr"] = f"codex exit {ocr['codex_returncode']}; abort. Codex stderr tail:\n{err_tail}"
                else:
                    summary["stages"]["ocr"] = f"codex exit {ocr['codex_returncode']}; abort"
                summary["aborted"] = True
                _log_step_done("3/8", "stage+ocr (failed)", t0)
                return summary
            summary["stages"]["ocr"] = "ok"
            summary["transcripts"] = [str(p) for p in (ocr["transcripts_copied_back"] or [])]
            _log_step_done("3/8", "stage+ocr", t0)
        else:
            print(f"  [dry-run] Would stage {len(photo_paths)} photos and call codex_lane", flush=True)
            summary["stages"]["ocr"] = "dry-run"
            _log_step_done("3/8", "stage+ocr (dry-run)", t0)

        # Step 3: marking pass
        print("=" * 60, flush=True)
        print(f"Step 4/8: marking pass for {slug}", flush=True)
        print("=" * 60, flush=True)
        _log_step_start("4/8", "marking")
        t0 = time.time()
        if not dry_run:
            if engine == "ollama":
                # Ollama vision path - no Codex
                mark = ollama_vision.run_marking(slug=slug)
            else:
                mark = mark_batch.run_marking(
                    slug=slug,
                    job_name=f"mark-{slug}",
                    yes=True,
                    skip_copy_back=False,
                )
            if mark["codex_returncode"] != 0:
                err_tail = mark.get("codex_err_tail", "").strip()
                if err_tail:
                    summary["stages"]["marking"] = f"codex exit {mark['codex_returncode']}; abort. Codex stderr tail:\n{err_tail}"
                else:
                    summary["stages"]["marking"] = f"codex exit {mark['codex_returncode']}; abort"
                summary["aborted"] = True
                _log_step_done("4/8", "marking (failed)", t0)
                return summary
            summary["stages"]["marking"] = "ok"
            summary["marking_files"] = [str(p) for p in (mark["marking_files_copied_back"] or [])]
            summary["tally"] = mark.get("tally")
            _log_step_done("4/8", "marking", t0)
        else:
            print(f"  [dry-run] Would call codex_lane for marking", flush=True)
            summary["stages"]["marking"] = "dry-run"
            _log_step_done("4/8", "marking (dry-run)", t0)
    else:
        summary["stages"]["ocr"] = "skipped"
        summary["stages"]["marking"] = "skipped"

    # Step 4: publish
    print("=" * 60, flush=True)
    print(f"Step 5/8: publish (render pages/assessments/{slug}.html)", flush=True)
    print("=" * 60, flush=True)
    _log_step_start("5/8", "publish")
    t0 = time.time()
    if not dry_run:
        student = publish.read_student_json(require_recipient=False)
        # Engine label: what the footer renders next to "Using X for OCR".
        # The user sees this on the published page; pick the friendly
        # names here so the pipeline can stay engine-agnostic
        # internally (it just passes "codex" or "ollama").
        engine_label = "ChatGPT (Codex pass)" if engine == "codex" else "Ollama (qwen3.5:397b)"
        # First-render timestamp. Distinct from the page's
        # "Last updated" line (which refreshes on every feedback
        # PUT). This is the moment the assessment went live.
        import datetime as _dt
        published_at_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        meta = publish.publish_one(
            slug, student,
            dry_run=False,
            engine_label=engine_label,
            published_at_iso=published_at_iso,
        )
        publish.publish_index(
            [meta],
            dry_run=False,
            engine_label=engine_label,
            published_at_iso=published_at_iso,
        )
        publish.copy_assets(dry_run=False)
        publish.copy_photos(slug, dry_run=False)
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

    # Step 5: auto-commit + push
    print("=" * 60, flush=True)
    print("Step 6/8: git auto-commit + push to origin/main", flush=True)
    print("=" * 60, flush=True)
    _log_step_done("5/8", "publish", t0)
    _log_step_start("6/8", "git_push")
    t0 = time.time()
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
                _log_step_done("6/8", "git_push", t0)
            except subprocess.CalledProcessError as e:
                print(f"  git push failed: {e}", flush=True)
                summary["stages"]["git"] = f"failed: {e}"
                summary["aborted"] = True
                _log_step_done("6/8", "git_push (failed)", t0)
                return summary
    else:
        print("  [dry-run] Would git add + commit + push to origin/main", flush=True)
        summary["stages"]["git"] = "dry-run"
        _log_step_done("6/8", "git_push (dry-run)", t0)

    # Step 6: wait for the Pages workflow to deploy (so the public
    # URL is live when the email lands).
    print("=" * 60, flush=True)
    print("Step 7/8: wait for Pages deploy", flush=True)
    print("=" * 60, flush=True)
    _log_step_start("7/8", "pages_deploy")
    t0 = time.time()
    if not dry_run:
        if wait_for_pages_deploy(slug, timeout_sec=120):
            summary["stages"]["pages_deploy"] = "ok"
            _log_step_done("7/8", "pages_deploy", t0)
        else:
            print("  Pages deploy not seen within 120s; sending email anyway", flush=True)
            summary["stages"]["pages_deploy"] = "timeout"
            _log_step_done("7/8", "pages_deploy (timeout)", t0)
    else:
        print("  [dry-run] Would poll the latest workflow run for completion", flush=True)
        summary["stages"]["pages_deploy"] = "dry-run"
        _log_step_done("7/8", "pages_deploy (dry-run)", t0)

    # Step 7: send the email
    print("=" * 60, flush=True)
    print(f"Step 8/8: send email ({to_mode})", flush=True)
    print("=" * 60, flush=True)
    _log_step_start("8/8", "email")
    t0 = time.time()
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
            _log_step_done("8/8", "email", t0)
        except Exception as e:
            print(f"  email send failed: {e}", flush=True)
            summary["stages"]["email"] = f"failed: {e}"
            summary["aborted"] = True
            _log_step_done("8/8", "email (failed)", t0)
            return summary
    else:
        print(f"  [dry-run] Would send email to {to_mode} recipient via Gmail SMTP", flush=True)
        summary["stages"]["email"] = "dry-run"
        _log_step_done("8/8", "email (dry-run)", t0)

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
    # Try the dedicated PAT targets first, then fall back to the
    # git-credential-manager blob (which stores protocol/host/
    # username/password in a multi-line string). The PAT IS
    # there -- it's just stored under the git helper's target
    # name "git:https://github.com", not under "github:pat" /
    # "github-token".
    token = _read_credential("github:pat") or _read_credential("github-token")
    if not token:
        git_blob = _read_credential("git:https://github.com")
        if git_blob:
            for line in git_blob.splitlines():
                if line.startswith("password="):
                    token = line.split("=", 1)[1].strip()
                    break
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
    p.add_argument("--slug", default=None, help="Paper slug. Optional when --auto-discover is set.")
    p.add_argument(
        "--photos", nargs="*", type=Path, default=[],
        help="Photo paths in glob order. Required unless --skip-codex or --auto-discover is set.",
    )
    p.add_argument(
        "--page-order", type=int, nargs="*", default=None,
        help="Printed page number for each photo, in --photos order. Gaps are fine. "
             "Optional when --auto-discover is set.",
    )
    p.add_argument("--to", choices=("staging", "live"), default="staging",
                   help="Which recipient to email. Default staging (Aaron).")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    p.add_argument("--dry-run", action="store_true",
                   help="Do every step except the destructive ones (no push, no email send, no git commit).")
    p.add_argument("--skip-codex", action="store_true",
                   help="Skip OCR and marking. Useful for re-running publish + email when marking is already done.")
    p.add_argument("--auto-discover", action="store_true",
                   help="Run a Codex discovery pass to identify the paper slug and printed page "
                        "order from the photos themselves. The slug and page-order arguments "
                        "become optional; the orchestrator picks the most recent N photos from "
                        "the gateway cache (N from --photos-hint, or all of them).")
    p.add_argument("--photos-hint", type=int, default=None,
                   help="Expected number of photos for --auto-discover. If omitted, the "
                        "orchestrator uses every photo currently in the gateway cache.")
    p.add_argument(
        "--engine", default="ollama", choices=["codex", "ollama"],
        help="LLM backend: 'codex' (default, original sandbox path) or "
             "'ollama' (calls Ollama directly with photos as inline "
             "image attachments, no sandbox). Currently affects the "
             "auto-discover step; OCR + marking still use Codex.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if not args.skip_codex and not args.auto_discover and not args.photos:
        print("ERROR: --photos is required unless --skip-codex or --auto-discover is set.", file=sys.stderr)
        return 1
    if not args.auto_discover and not args.slug:
        print("ERROR: --slug is required unless --auto-discover is set.", file=sys.stderr)
        return 1

    if not args.yes and not args.dry_run:
        print(f"This will run the full pipeline for {args.slug or '(auto-discover)'}:", flush=True)
        if args.auto_discover:
            print(f"  mode: auto-discover (slug + page order from Codex discovery pass)")
            print(f"  photos hint: {args.photos_hint or 'all photos in cache'}")
        else:
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
        auto_discover_mode=args.auto_discover,
        photos_hint=args.photos_hint,
        engine=args.engine,
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
