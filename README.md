# the-examiner

A second-pair-of-eyes GCSE marker, built for a single GCSE student
(this repo refers to them as "the student" throughout — see
`docs/PRIVACY.md` for why). The pipeline ingests GCSE question
papers and mark schemes, runs structured extraction on them, then
(once the student submits photos of their answers) runs OCR + marking
on the photos and publishes per-question results to GitHub Pages
for the student to review, followed by an email to the active
identity and (when `active.json=student`) to the student.

This README describes the *current* state of the pipeline. The
operator's view (gotchas, recovery procedures, observability) lives
in `docs/PIPELINE.md`. The setup walkthrough is in `docs/SETUP.md`.
The data model for `paper.json` / `markscheme.json` is in
`docs/SCHEMA.md`.

## The walk-away flow (2026-06-16)

The headline change in this commit window is the **walk-away
end-to-end flow**: drop QP+MS PDFs into a Drive-mirrored folder,
send `/mark N` on Telegram, the orchestrator does the rest. No
manual steps in between.

```
[user] drop QP+MS into exam-papers/ (Drive mirror)
   ↓
[user] /mark 26 on Telegram
   ↓
[hook] parse + spawn wrapper (fire-and-forget)
   ↓
[orchestrator] 8 steps, fully observable, fully automated
   ↓
[email]   staging email with live URL
```

Wall-clock for a fresh 26-page batch:

- Step 0 (papers_sync + extract): 5-15 min if `papers/` is
  virgin (LLM cold), 0-5 min if recently indexed (cache hot)
- Step 1 (auto-discover): ~30s (Codex sandbox)
- Step 3 (stage + OCR): 1-3 min (Codex sandbox)
- Step 4 (mark): 1-3 min (Codex sandbox)
- Step 5 (publish): <10s (in-process)
- Step 6 (git push): ~5s
- Step 7 (Pages deploy): 30-60s
- Step 8 (email): ~5s

**Total: ~10-25 min for a fresh batch, 5-10 min for a repeat.**

## Status (2026-06-16)

| Phase | What it does | Status |
|---|---|---|
| 0a. Papers sync | Sync PDFs from the Drive-mirrored `exam-papers/` folder into `papers/` (filename-dedup) | ✅. Built, committed (`07c01cb`) |
| 0b. Indexer | Run `index_papers.py` to derive slugs, write `meta.{qp,ms}.json` + `pair.json` + `kvdb-bucket.txt` + `raw/<basename>.txt`, update `index/papers.json` | ✅. Built, committed (`b34230d`) |
| 0c. Extractor | Run `extract_questions.py` for any slug missing `paper.json` or `markscheme.json`. LLM call to `minimax-m3:cloud`. Atomic write (`_write_json_atomic`). | ✅. Built, committed (`07c01cb`) |
| 1. Auto-discover | Identify the paper + printed page order from the photos themselves. Codex in a disposable sandbox. Picks the most recent N photos from the cache by LastWriteTime. | ✅. Built, validated (2026-06-15) |
| 2. Markscheme check | Verify `papers/<slug>/markscheme.json` exists. If missing, abort with email to the active identity, do not push, do not email the student. | ✅. Built, validated (2026-06-15) |
| 3. Stage + OCR | Stage photos into `intake/<slug>/` named by printed page number. Run Codex OCR in a sandbox. Per-page transcripts returned to `intake/<slug>/<page>.transcript.md`. | ✅. Built, validated (2026-06-15) |
| 4. Marking | Per-criterion Codex marking against the indexed `markscheme.json` rubric. Output: `intake/<slug>/Q01.marking.md` ... `Q09.marking.md` + `SUMMARY.md`. | ✅. Built, validated on `aqa-84621h-chemistry-higher-2024-05` (67/100, 2026-06-16) |
| 5. Publish | Render `pages/assessments/<slug>.html` with per-criterion answer-photo thumbnails (the `df90c72` feature), per-criterion verdicts, lightbox, legibility scores, feedback bucket embed. | ✅. Built, deployed (Pages live at `https://undert0e-505.github.io/the-examiner/`) |
| 5a. Per-criterion answer-photo thumbnails | Auto-generate thumb block in the published HTML (the 20484fa manual-paste approach is dead). Each criterion gets a thumb of the student's answer page; click for lightbox. | ✅. Built, committed (`df90c72`) |
| 5b. Bucket self-heal | If KVdb 404s (long-idle bucket GC'd by kvdb.io), the orchestrator creates a new one and writes the new id to `kvdb-bucket.txt` | ✅. Built, validated (2026-06-15) |
| 6. Git auto-commit + push | Auto-commit the rendered page + thumb assets, push to `origin/main`. Push safety rail (`HEAD:refs/heads/main` + post-push verify) since `8d02139`. | ✅. Built, validated |
| 7. Pages deploy wait | Poll the latest Pages workflow run, wait up to 120s for green. If timeout, send email anyway. | ✅. Built, validated |
| 8. Email | Send to the active identity (always) + the student (only when `active.json=student` AND `--to live`) the assessment URL. | ✅. Built, validated (2026-06-15) |
| /mark hook | Bridge from Telegram `/mark N` to the orchestrator. Buffer photos for the count, fire the wrapper. Fire-and-forget. | ✅. Built, validated end-to-end (2026-06-16) |
| Orchestrator observability | Step-level (`step_start` / `step_done` with ISO timestamps) and LLM-call-level (`POST` / `response` / `TIMEOUT`) logging. Each step banner now has a wall-clock duration; each `requests.post` is bracketed with timing. | ✅. Built, committed (`04b8d11`) |
| Push safety rail | `git push origin HEAD:refs/heads/main` + post-push verify (raises `RuntimeError` if `origin/main` didn't actually move). Replaces the silent-no-op bug that stranded 69/100 on `m3-adapter` for ~90 min. | ✅. Built, committed (`8d02139`) |
| Feedback harvester | Poll the student's per-mark responses from KVdb, write `corrections.md`, update `calibration/<subject>.md` | ✅. Built (Phase 3 feedback loop) |
| Pre-commit PII blocklist | `scripts/pre-commit` blocks the names "Will" and "Aaron" in staged content (case-insensitive whole-word). Exempts `src/publish.py`, `src/run.py`, `src/papers_sync.py` (internal doc references). | ✅. Built, validated (2026-06-15, updated 2026-06-16) |

## Layout

```
the-examiner/
├── README.md                       # this file -- the high-level tour
├── papers/                         # QP+MS PDFs + per-slug indexed data
│   ├── <PDF>                       # the source-of-truth PDFs
│   └── <slug>/                     # one dir per pair, written by index_papers.py
│       ├── meta.qp.json
│       ├── meta.ms.json
│       ├── pair.json
│       ├── paper.json              # written by extract_questions.py
│       ├── markscheme.json         # written by extract_questions.py
│       ├── kvdb-bucket.txt
│       └── raw/                    # per-page text dump, gitignored
├── index/
│   └── papers.json                 # master list of all known papers
├── pages/                          # generated, deployed to GitHub Pages
│   ├── assessments/<slug>.html     # per-assessment page
│   ├── assets/                     # CSS, JS, photos
│   └── index.html                  # the per-batch index
├── intake/                         # photos staged for OCR + mark
│   ├── <slug>/<page>.jpg           # named by printed page number
│   ├── <slug>/<page>.transcript.md
│   └── <slug>/<page>.marking.md
├── assessments/                    # per-batch run summaries
├── transcripts/                    # intermediate OCR dumps
├── calibration/                    # per-subject calibration data
├── private/                        # owner/student config (NEVER COMMITTED)
│   ├── active.json
│   ├── aaron.json
│   └── student.json
├── src/                            # pipeline scripts (see src/README.md)
│   ├── run.py                      # the orchestrator
│   ├── papers_sync.py              # Drive → papers/ sync + ensure indexed
│   ├── index_papers.py             # Phase 1: PDF → meta + pair + bucket
│   ├── extract_questions.py        # Phase 2: raw text → paper.json + markscheme.json (LLM)
│   ├── llm.py                      # minimal Ollama chat wrapper
│   ├── ocr_batch.py                # Phase 3a: codex_lane OCR wrapper
│   ├── mark_batch.py               # Phase 3b: codex_lane marking wrapper
│   ├── discover_batch.py           # auto-discover (slug + page order from photos)
│   ├── publish.py                  # Phase 4: pages/ HTML render
│   ├── send_email.py               # Phase 5: Gmail SMTP
│   ├── photo_discovery.py
│   ├── parse_trigger.py            # /mark <slug> photos=N order=... parser
│   ├── generate_prompts.py
│   ├── backends/
│   └── prompts/                    # LLM prompt templates (the IP)
├── tests/                          # regression tests
├── docs/                           # operator docs
│   ├── PIPELINE.md                 # gotchas, recovery, observability
│   ├── SETUP.md                    # first-time setup
│   ├── SCHEMA.md                   # paper.json + markscheme.json data model
│   ├── PRIVACY.md                  # what we never publish
│   └── ...                         # prompt templates, design notes
├── scripts/
│   └── pre-commit                  # PII blocklist + bypass for internal docs
└── logs/                           # per-step log files (gitignored)
    ├── pipeline-<timestamp>.log
    ├── run-<timestamp>.log
    └── ...
```

## The /mark hook (the new front door)

The hook is a user-managed internal hook at
`~/.openclaw/hooks/mark-pipeline-trigger/`. It listens for
`message:received` Telegram events, parses `/mark N` (just the
count), and spawns `D:\dev\openclaw-scripts\run-pipeline-with-log.cmd
--auto-discover --photos-hint N --to staging --yes` as a
fire-and-forget child process.

The wrapper is the logging primitive — every run produces a
timestamped log file at `logs/pipeline-YYYY-MM-DD-HHMMSS.log`
regardless of whether the user is invoking from Telegram,
a hand-typed PowerShell prompt, a Scheduled Task, or a cron job.

The hook is intentionally minimal. The actual waiting-for-photos
logic, the discovery pass, the per-photo intake staging, the OCR,
the marking, the publish, the push, the deploy, the email — all
of that lives inside `src/run.py` when invoked with `--auto-discover`.
The hook is a thin pass-through.

## What changed in 2026-06-16

The walk-away flow + observability work:

- **`07c01cb` — feat(pipeline)**: auto-sync PDFs from `exam-papers/`,
  atomic-write, new Step 0 in `run.py`. The `papers_sync.py` module.
- **`7ab6c92` — chore(pipeline)**: rename `gcs-papers/` →
  `exam-papers/` (real path on the host, the folder's
  actual name).
- **`cccb6d0` — fix(papers_sync)**: dry-run no longer lies about
  rebuilding the index (the `--clean` dead code + the `indexed=True`
  always-set bug).
- **`04b8d11` — feat(observability)**: step-level + LLM-call
  timing in `run.py` + `extract_questions.py` + `llm.py`. The
  log file self-describes where each stage is and how long it took.
- **Hook** (`~/.openclaw/hooks/mark-pipeline-trigger/`): the
  `HOOK.md` + `handler.ts` that bridge Telegram `/mark` to the
  wrapper. Discovered by the gateway, enabled in config, requires
  a gateway restart to load.
- **Wrapper** (`D:\dev\openclaw-scripts\run-pipeline-with-log.cmd`):
  the timestamped-log wrapper that all invocations route through.

## Operator notes

For the day-to-day — "the orchestrator is stuck, what do I do?"
or "I want to re-run the publish + email without re-marking" —
see `docs/PIPELINE.md`. It has the observability cheatsheet, the
recovery procedures, the skip flags, and the known gotchas.

For first-time setup — Drive mirror, Windows Credential Manager,
PAT scopes, `OLLAMA_KEEP_ALIVE` — see `docs/SETUP.md`.

For the per-file data model — what `paper.json` and
`markscheme.json` look like, what fields are required, what
fields are optional, what the schema validates — see
`docs/SCHEMA.md`.

## License

Internal / private. Not open source. Not for redistribution.
The student name + email used in the published pages are read
from `private/student.json` and never appear in this repo's
committed history (see `docs/PRIVACY.md`).
