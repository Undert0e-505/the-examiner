# PIPELINE.md

How to run the the-examiner auto-marker end-to-end: invocation,
intermediate states, outputs, and how to switch the target student.
Read this top-to-bottom before kicking off a run.

This is the **operational** doc. The **architecture** doc is
`README.md`. The **privacy policy** is `docs/PRIVACY.md`. The
**one-time setup** doc is `docs/SETUP.md`. The **regression
procedure** is `docs/REGRESSION-RUN.md`. Don't duplicate their
content here — link to them.

---

## TL;DR

```powershell
cd D:\dev\the-examiner

# 1. Read the photos in the gateway cache. The 26 chemistry
#    photos are already there (Telegram media cache at
#    C:\Users\openclaw-agent\.openclaw\media\inbound\).

# 2. Kick off the orchestrator. The orchestrator reads the latest
#    26 photos from the cache, identifies the paper slug + page
#    order from the photos themselves, and runs OCR + marking +
#    publish + push + email.
D:\Python310\python.exe src\run.py --auto-discover --photos-hint 26 --yes
```

Expected runtime: **20-25 min**. Expected result: **71/100 (71%)**
on the AQA GCSE Chemistry Higher Paper 1 (8462/1H, May 2024).
Email goes to `aaronjoakley55@gmail.com` (the staging recipient
for the default `aaron` identity).

The orchestrator is the **only** entry point you need. Don't
invoke `ocr_batch.py`, `mark_batch.py`, or `publish.py` directly
for end-to-end runs — they expect a previous stage to have set
up state, and a manual call will break the pipeline.

---

## How to invoke the pipeline

The orchestrator is `src/run.py`. It accepts these flags:

| Flag | Required? | What it does |
|---|---|---|
| `--slug SLUG` | yes, unless `--auto-discover` | Paper slug. Must match a `papers/<slug>/` folder with `paper.json` and `markscheme.json` already extracted. |
| `--photos PATH [PATH ...]` | yes, unless `--auto-discover` or `--skip-codex` | Photo paths in glob order. The orchestrator passes them to the OCR stage. |
| `--page-order N [N ...]` | optional | Printed page number for each photo, in `--photos` order. Gaps are fine (the chemistry paper skips 13, 16, 18). Used by the OCR stage to name transcripts. **Required** if `--photos` is given and the photos aren't pre-named with page numbers. |
| `--auto-discover` | optional | Run a Codex discovery pass on the photos themselves to identify the slug and page order. The orchestrator picks the latest N photos from the gateway cache (N from `--photos-hint`, or all of them). **Use this when the user just sent photos via Telegram with no slug info.** |
| `--photos-hint N` | optional, with `--auto-discover` | Expected number of photos. The orchestrator picks the latest N from the cache. The discovery pass will then verify by looking at the photos. **Recommended** — bare `--auto-discover` is more fragile if the cache has stale photos from other batches. |
| `--to {staging,live}` | optional, default `staging` | Which recipient to email. `staging` goes to the identity's `recipient_email_staging` (always Aaron's). `live` goes to the identity's `recipient_email_live` (the student's email, when `active=student`). |
| `--yes` | optional | Skip the confirmation prompt. **Required for non-interactive use.** |
| `--dry-run` | optional | Do every step except the destructive ones (no push, no email send, no git commit). Output is logged but nothing changes on disk or remote. |
| `--skip-codex` | optional | Skip OCR and marking. Use when the marking is already done and you want to re-render + re-push + re-email. Goes straight to stage 4 (publish). |

### Common invocation shapes

```powershell
# Full auto-discover from Telegram photos (the regression test path)
D:\Python310\python.exe src\run.py --auto-discover --photos-hint 26 --yes

# Re-render + re-push + re-email without redoing OCR/marking
D:\Python310\python.exe src\run.py --slug aqa-84621h-chemistry-higher-2024-05 --skip-codex --yes

# Dry-run for verification
D:\Python310\python.exe src\run.py --slug aqa-84621h-chemistry-higher-2024-05 --dry-run --skip-codex --yes

# Send the live email to the student (only when active=student)
D:\Python310\python.exe src\run.py --slug aqa-84621h-chemistry-higher-2024-05 --to live --yes
```

---

## What happens at each stage

The orchestrator is 8 stages (plus an optional stage 0). Each
stage is gated on the previous one; if a stage fails, the
pipeline aborts and emails Aaron the abort reason.

| # | Stage | What it does | Time | Intermediate output |
|---|---|---|---|---|
| 0 | **Auto-discover** (only with `--auto-discover`) | Pick the latest N photos from the gateway cache. Run Codex in a disposable sandbox to identify the paper slug + printed page order. Restage the photos into `intake/<slug>/<page>.jpg` (clearing any pre-existing .jpg first). | ~4 min | `intake/<slug>/{01,02,...,29}.jpg` |
| 1 | **Markscheme check** | Confirm `papers/<slug>/markscheme.json` exists. Abort with email if missing. | instant | (no output) |
| 2 | **OCR** | Run Codex in a disposable sandbox with the photos and a per-page OCR prompt. Codex reads the handwritten answer on each page, transcribes it verbatim (including spelling mistakes, `[illegible]` for unreadable text). | ~10 min | `intake/<slug>/{01,...,29}.transcript.md` (26 files) |
| 3 | **Marking** | Run Codex in a disposable sandbox with the transcripts, the markscheme, and a per-Q marking prompt. Codex evaluates each criterion, writes per-Q `Q01-Q09.marking.md` files, a `SUMMARY.md` rollup, and a per-Q legibility score (0-5). | ~6 min | `assessments/<slug>/Q01-Q09.marking.md` + `SUMMARY.md` |
| 4 | **Publish** | Render `pages/assessments/<slug>.html` from the marking files. Includes the design system (warm orange accent, two-column desktop with sticky rail, mobile compact rail card). The page is the per-assessment view that the student reads. | ~1 min | `pages/assessments/<slug>.html` (~140 KB) + `pages/index.html` + `pages/assets/css/styles.css` + `pages/assets/js/feedback.js` |
| 5 | **Git auto-commit + push** | Commit the new assessment + page + assets, push to `main`. Triggers the GitHub Actions `static.yml` workflow, which deploys the new page to GitHub Pages. | ~1 min | new commit on `main` (e.g. `d1719a6 publish: render assessment HTML ...`) |
| 6 | **Pages deploy wait** | Poll the GitHub Actions API for the `static.yml` workflow run on the new commit. Wait up to 120s for green. **Best-effort** — if the GitHub PAT is missing in the Credential Manager, this stage is skipped and the email goes out anyway. | 1-2 min (or skip) | (no output) |
| 7 | **Email** | SMTP send via `jimothyoakley55@gmail.com` (Jimothy's Gmail account, app password in Windows Credential Manager). Staging goes to Aaron. Live (when `active=student`) goes to the student + cc's Aaron. | instant | (no output) |
| 8 | **Bucket self-heal** | If the KVdb bucket 404s (kvdb.io GCs buckets after long idle), the orchestrator creates a new one and writes the new id to `papers/<slug>/kvdb-bucket.txt`. The student-feedback widget on the live page picks up the new bucket via `data-kvdb-bucket` on the next page load. | instant (on failure only) | updated `papers/<slug>/kvdb-bucket.txt` |

### Where things live during and after a run

```
papers/<slug>/             # Source of truth (extracted per Q + criteria, markscheme, bucket id)
intake/<slug>/             # Photos from Telegram, page-numbered by auto-discover (gitignored)
  *.jpg                    #   the photos
  *.transcript.md          #   OCR'd pages, written by the OCR stage (gitignored)
  *.transcript.md.bak      #   pre-existing transcripts, kept as .bak (gitignored)
assessments/<slug>/        # Marking files, written by the marking stage (gitignored)
  Q01-Q09.marking.md       #   per-Q criterion verdicts + justifications
  Q01-Q09.marking.md.bak   #   pre-existing marking files, kept as .bak
  SUMMARY.md               #   per-Q tally table + Cross-paper observations + Assessor notes
pages/                     # GitHub Pages output, the public site
  assessments/<slug>.html  #   the per-assessment view, regenerated on each publish
  index.html               #   the dashboard, lists all assessments
  assets/                  #   CSS + JS, copied from src/assets/ on each publish
```

`intake/`, `assessments/`, `transcripts/`, and the per-student
`private/` config are all **gitignored** — they live on this
machine only, never on the public repo. `pages/assessments/`
is **explicitly whitelisted** in `.gitignore` because Pages
needs the files in git to deploy them.

---

## What the final output looks like

After a successful run, you'll have:

1. **A live page on GitHub Pages** at
   `https://undert0e-505.github.io/the-examiner/assessments/<slug>.html`.
   The page shows:
   - The hero with the headline score (e.g. "71 / 100")
   - The per-question breakdown (9 collapsible sections, one
     per question, each with the question text, the criteria,
     the marks awarded vs available, the justification, and a
     "Do you agree with this mark?" feedback widget)
   - The "Cross-paper observations" callout (3-5 short
     paragraphs on cross-cutting patterns the student showed)
   - A collapsed "Assessor notes" details element at the very
     bottom (pipeline meta: OCR blockers, legibility overview,
     pipeline verdict — for Aaron's eyes, not the student's)

2. **An email** to `aaronjoakley55@gmail.com` with:
   - Subject: `Your 8462/1H result is ready — 71/100 (71%)`
   - A short per-Q tally (Q01 9/10, Q02 11/11, ...)
   - A link to the live page
   - No per-criterion details (the page is the workspace; the
     email is the notification)

3. **A new commit on `main`** with a message like
   `publish: render assessment HTML for <slug> — 71/100 (71%)`.
   This triggers the GitHub Actions `static.yml` workflow that
   deploys the page.

---

## How to switch the target student

The system is built for one student. The student's full name,
email, and PII live in `private/student.json` (gitignored).
**Live send to the student is gated by two things**:

1. **Active identity** in `private/active.json`. The default is
   `aaron` (which means emails go to Aaron). To send to the
   student, set `active.json` to `{"active": "student"}`.
2. **`--to live` flag** in the orchestrator invocation. Even
   with `active=student`, `--to staging` still goes to Aaron.
   `--to live` is the only way to email the student.

The two are designed to be independent. The typical workflow:

```powershell
# Default (Aaron sees everything, student sees nothing):
D:\Python310\python.exe src\run.py --slug <slug> --yes
# (--to defaults to staging)

# Live to the student (with Aaron cc'd):
# Step 1: flip the identity
Set-Content private\active.json -Value '{"active": "student"}'
# Step 2: run with --to live
D:\Python310\python.exe src\run.py --slug <slug> --to live --yes
# Step 3: flip back
Set-Content private\active.json -Value '{"active": "aaron"}'
```

**Safety rail**: the per-identity `recipient_email_staging` field
overrides the `--to` flag. If `active=student` and you pass
`--to staging`, the email still goes to Aaron's address (because
`student.json:recipient_email_staging = aaronjoakley55@gmail.com`).
This is intentional — `--to live` is the only way to email the
student, even with the flag flipped.

### Identity files (all gitignored, on this machine only)

```
private/
├── active.json     # {"active": "aaron"} or {"active": "student"}
├── aaron.json      # Aaron's identity: name, email, salutation, signoff
├── student.json    # The student's identity: name, email, salutation, signoff
└── README.md       # Operator's manual for the identity system
```

The operator's manual in `private/README.md` is the source of
truth for what's in each file. The `student.json` file ships
with the student's real identity baked in (the repo operator
sets it up once on first clone). If you need to change the
student (different student, or the student moves to a different
email), edit `private/student.json` directly — it's gitignored
so the change is local-only.

---

## How to know it's working

Watch the log output as the orchestrator runs. The successful
sequence of messages:

```
Step 0/8: auto-discover (paper + page order)
  wait_for_photos: cache has 95 photos (waiting for 26)
  Auto-discover: picked 26 photos from C:\Users\openclaw-agent\.openclaw\media\inbound
  Staged 26 photos into D:\dev\the-examiner\intake\_discover\discover-XXXXXXXXXX
  ...
  [4 min of Codex exec]
  ...
  Codex exec finished. Exit code: 0
  Discovered slug: aqa-84621h-chemistry-higher-2024-05
  Restaged 26 photos into intake/aqa-84621h-chemistry-higher-2024-05/
  discovered slug=..., 26 photos, page_order=[1, 2, ..., 12, 14, 15, 17, 19, ..., 29]
Step 1/8: markscheme check for aqa-84621h-chemistry-higher-2024-05
  markscheme.json present at D:\dev\the-examiner\papers\.../markscheme.json
Step 2/8: stage 26 photos to intake/aqa-84621h-chemistry-higher-2024-05/
  ...
  [~10 min of Codex exec]
  ...
  Copied 26 transcripts back
Step 3/8: marking pass for aqa-84621h-chemistry-higher-2024-05
  intake/.../ has 26 transcripts
  papers/.../markscheme.json is in place
  ...
  [~6 min of Codex exec]
  ...
Step 4/8: publish (render pages/assessments/<slug>.html)
  rendered D:\dev\the-examiner\pages\assessments\<slug>.html  (~140000 bytes, 9 questions)
  rendered D:\dev\the-examiner\pages\index.html
  copied D:\dev\the-examiner\src\assets\styles.css -> ...
  copied D:\dev\the-examiner\src\assets\js\feedback.js -> ...
Step 5/8: git auto-commit + push to origin/main
  $ git add pages/assessments/<slug>.html
  $ git add pages/index.html
  $ git add pages/assets/css/styles.css
  $ git add pages/assets/js/feedback.js
  $ git add papers/<slug>/kvdb-bucket.txt
  $ git commit -m publish: render assessment HTML for <slug> ...
  Pushing to origin/main (Pages deploy will follow)...
  $ git push origin main
Step 6/8: wait for Pages deploy
  No GitHub PAT in Credential Manager; skipping deploy wait
Step 7/8: send email (staging)
Step 8/8: bucket self-heal (only on failure)

PIPELINE SUMMARY
  auto_discover: ok
  markscheme_check: ok
  ocr: ok
  marking: ok
  publish: ok
  git: ok
  pages_deploy: timeout (or ok)
  email: sent to aaronjoakley55@gmail.com
```

If any stage shows `codex exit 2; abort` or `codex exit 1; abort`,
the pipeline aborts. **Read the log immediately above the abort**
for the specific reason. The most common aborts in practice are:

- **Stale Codex sandbox dir** — the wrapper refuses to overwrite
  an existing sandbox. Fixed: pass `-Force` to the wrapper
  (orchestrator already does this as of 2026-06-15; if you see
  this abort, the wrapper version on disk may be out of date.
  Check the wrapper at
  `D:\dev\openclaw-scripts/codex_lane/run_codex_sandbox_job.ps1`
  is v0.2.2 or later.)
- **Markscheme missing** — `papers/<slug>/markscheme.json` doesn't
  exist. Re-run the indexer + extractor:
  `python src\extract_questions.py <slug>`.
- **Bucket 404 on Pages** — kvdb.io GC'd the bucket. The orchestrator's
  bucket self-heal will catch this and write a new id to
  `papers/<slug>/kvdb-bucket.txt`.

---

## How to know it succeeded

After the orchestrator exits with code 0, verify:

1. **The live page is up and has the new run's data**: open
   `https://undert0e-505.github.io/the-examiner/assessments/<slug>.html`
   in a browser. Hard-reload to bypass CDN cache. The hero
   should show the new total. Scroll to the bottom — the
   "Assessor notes" details element should be there.
2. **The email landed**: check `aaronjoakley55@gmail.com`. Subject
   should be `Your 8462/1H result is ready — 71/100 (71%)` (or
   similar for the actual paper).
3. **The bucket is responsive**: open the live page, expand a
   criterion, click "Agree" or "Disagree" on a feedback widget.
   The "Save feedback" button should write to the bucket. Check
   `https://kvdb.io/<bucket-id>/student-feedback` (bucket id is
   in `papers/<slug>/kvdb-bucket.txt`).

If all three checks pass, the run is good.

---

## Re-running the same paper

The orchestrator is **idempotent at the page level** (running
twice produces the same rendered HTML, modulo Codex
nondeterminism on a fresh sandbox) but **destructive at the
working-folder level** (a re-run overwrites the photos, transcripts,
and marking files in `intake/<slug>/` and `assessments/<slug>/`).

To re-run safely:

1. **Back up the current state** if you want to preserve it:
   `cp -r intake/<slug> intake/_backups/<slug>-<timestamp>` and
   `cp -r assessments/<slug> assessments/_backups/<slug>-<timestamp>`.
2. **Re-fire the orchestrator**:
   `D:\Python310\python.exe src\run.py --auto-discover --photos-hint 26 --yes`
3. **Compare** the new run's `assessments/<slug>/SUMMARY.md`
   against the backup. Per-Q totals should match (modulo ±2
   marks from Codex nondeterminism). Per-Q legibility scores
   should match (4, 4, 3, 3, 3, 4, 1, 3, 3 for the chemistry
   paper).

The pre-existing transcripts and marking files get auto-backed
up to `*.transcript.md.bak` and `*.marking.md.bak` in the
respective folders before the new run overwrites them. If you
want a clean re-run (no .bak leftovers), `rm` the .bak files
after the run.

---

## Adding a new paper

The system supports multiple papers. To add a new one:

1. **Drop the QP and MS PDFs** in `papers/` (or any folder you
   have access to). The indexer picks up any `*.pdf` in the
   `papers/` tree.
2. **Run the indexer** to discover + slug + bucket:
   `D:\Python310\python.exe src\index_papers.py --clean`
   This creates `papers/<slug>/` with `pair.json`, `meta.qp.json`,
   `meta.ms.json`, and `kvdb-bucket.txt`.
3. **Run the extractor** to pull structured JSON out of the
   QP and MS:
   `D:\Python310\python.exe src\extract_questions.py`
   This creates `papers/<slug>/paper.json` (per-question entries)
   and `papers/<slug>/markscheme.json` (per-criterion entries).
4. **Kick off the orchestrator** as normal:
   `D:\Python310\python.exe src\run.py --auto-discover --yes`

The slug is content-derived: `<board>-<spec><paper>-<subject>-<tier?>-<YYYY>-<MM>`.
For example, `aqa-84621h-chemistry-higher-2024-05` = AQA, spec
8462, paper 1, Higher tier, May 2024.

Three papers are currently indexed (as of 2026-06-15):
`aqa-84621h-chemistry-higher-2024-05`, `aqa-87021-english-literature-2024-05`,
and `edexcel-1ma11h-mathematics-higher-2024-11`. Only the chemistry
paper has been run end-to-end; the other two are indexed and have
mark schemes extracted but no live assessments.

---

## See also

- `README.md` — architecture overview, design system, model choice
- `docs/PRIVACY.md` — what's private, what's public, the student's data
- `docs/SETUP.md` — one-time setup on a fresh clone
- `docs/REGRESSION-RUN.md` — the regression procedure
- `private/README.md` — identity-system operator's manual (gitignored)
- `src/README.md` — module-level docs (the per-stage scripts)
