# src/

The pipeline scripts. Each step is a small, runnable, reviewable unit.

## Scripts (built and committed)

| Script | Phase | Purpose |
|---|---|---|
| `index_papers.py` | 1 | Reads PDFs from `papers/`, walks the cover pages with regex (no LLM), writes per-pair metadata + the master `index/papers.json`. Assigns a stable UUIDv5 KVdb bucket per pair. |
| `extract_questions.py` | 2 | Reads the per-page raw text dumps from Phase 1, calls `minimax-m3:cloud` to extract per-question structure into `paper.json` and per-criterion structure into `markscheme.json`. |
| `llm.py` | shared | Minimal Ollama chat wrapper. `chat()` returns text, `chat_json()` requests `format: "json"` and validates against an ad-hoc schema. Retries on 5xx / 429 / connection errors with backoff. |
| `ocr_batch.py` | 3 | Thin wrapper around the `codex_lane` CLI for OCR runs. Stages photos into `intake/<slug>/`, runs Codex in a disposable sandbox with the verbatim-rule + graph-specific prompt, copies per-page transcripts back to the real repo. See `docs/OCR-PROMPT-TEMPLATES.md` for the prompt shape. |
| `mark_batch.py` | 3 | Thin wrapper around the `codex_lane` CLI for marking runs. Runs Codex in a disposable sandbox with the per-criterion marking prompt, copies per-question marking files back to the real repo, sums the totals. See `docs/MARKING-PROMPT-TEMPLATE.md` for the prompt shape. |

## Scripts (planned, not built)

| Script | Phase | Purpose |
|---|---|---|
| `intake.py` | 3 | Single-photo intake. The `ocr_batch.py` does the bulk-photo intake today; `intake.py` would handle the "one photo at a time" Telegram path. (The bulk path is what was actually built; the single-photo path is just `cp`.) |
| `match_paper.py` | 3 | Per-photo paper matching. OCR the photo header / context, fuzzy match against `index/papers.json`. Uncertain photos go to `intake/<batch>/unmatched/`. (For the 2024 AQA Chemistry run this was done by hand — Aaron told me the paper, I hard-coded the slug.) |
| `publish.py` | 3 | Generate the per-assessment HTML page for the student. Push to `main` to trigger Pages deploy (Pages still not enabled in repo settings; see `docs/SETUP.md` §4). The student name + email used in the page are read from `private/student.json` — see `docs/PRIVACY.md`. |
| `email.py` | 3 | Send the rendered email from `outbox/<batch>.txt` to Aaron + the student. The recipient email is read from `private/student.json`. |
| `poll_student_feedback.py` | 3 | Read the student's KVdb responses, write `corrections.md`, update `calibration/<subject>.md`. |

## Prompts (built and committed)

LLM prompts live in `prompts/` as plain-text files, versioned. They are
the actual IP of this project — when the marking improves, it's because
the prompts improved, not because the model got smarter.

| File | Phase | Purpose |
|---|---|---|
| `extract_qp.txt` | 2 | System + user prompt for QP extraction. Returns `{id, paper_question_number, section, extract, prompt, marks_available, page_start, page_end}` per question / sub-part. |
| `extract_ms.txt` | 2 | System + user prompt for MS extraction. Returns `{question_ref, paper_question_number, total_marks_for_question, criteria[]}` per question, where `criteria[].ao` is `AO1` / `AO2` / `AO3` / `AO4` / `null` (Edexcel Maths uses B/M/A process marks per step, not AOs). |

## Prompts (planned, not built)

- `transcribe.txt` — Pass 1 OCR of a photo of the student's handwriting. *Currently the prompt is constructed inline in the sandbox spec at `D:\dev\codex-sandboxes\_specs\ocr-batch-*-pages-*/04_CODEX_PROMPT.md`. Promoting those into a single versioned file in `prompts/` is a future cleanup; the doc `docs/OCR-PROMPT-TEMPLATES.md` has the template structure in the meantime.*
- `transcribe_with_context.txt` — Pass 2 OCR re-read. *Not built; the 1-pass design in `docs/OCR-ACCURACY.md` was sufficient for the first paper.*
- `mark.txt` — Final marking pass. *Same as above — currently constructed inline in the sandbox spec; doc is in `docs/MARKING-PROMPT-TEMPLATE.md`.*
- `calibrate.txt` — Meta-prompt for updating `calibration/<subject>.md`.
- `<subject>.txt` — Per-subject system context (one per of the 9 subjects).

## Conventions

- All scripts take a `--model` and `--base-url` flag (defaults to
  `minimax-m3:cloud` and `http://127.0.0.1:11434`). Override via
  `OLLAMA_MODEL` / `OLLAMA_BASE_URL` env vars.
- All scripts that talk to the LLM accept `--timeout` (default
  600 s) and `--max-retries` (default 1–2). The model has a long
  latency tail on large inputs (>30 KB can take 80–180 s); tune
  these if you're seeing flakiness.
- Scripts are idempotent on **structural** shape: re-running
  produces a `paper.json` / `markscheme.json` with the same keys,
  types, and entry counts. The LLM output itself is *not*
  bitwise deterministic even at temperature=0 — on a re-run, the
  verbatim prompts, indicative content, and even question
  splits can differ slightly (e.g. Edexcel Q2 might come out as
  one `q2` entry with 4 marks, or as `q2.1` / `q2.2` sub-parts
  with 3+1 marks). The committed files are *the* version; if a
  re-run produces a different shape, treat it as a fresh
  extraction and review the diff before committing.

## How `ocr_batch.py` and `mark_batch.py` actually work

These are **not standalone Python scripts that talk to an
LLM.** They are thin wrappers around the
`codex_lane` CLI, which runs the OpenAI Codex CLI in a
disposable sandbox. The actual LLM work is done by Codex
in the sandbox, with the prompt template in
`docs/OCR-PROMPT-TEMPLATES.md` or
`docs/MARKING-PROMPT-TEMPLATE.md` as the source of truth.

The Python wrapper exists to:

1. **Stage photos into `intake/<slug>/`** (file-system
   copy, not git clone, because the photos are deliberately
   untracked — AQA copyright).
2. **Invoke the codex_lane wrapper** with the right
   arguments: `-UseCopy` (not `git clone`), the right
   prompt-file path, the right job-name.
3. **Copy the produced transcripts / marking files back
   from the sandbox to the real repo.**

The wrapper is a few dozen lines of Python. The actual
prompt is in `D:\dev\codex-sandboxes\_specs\<job-name>/04_CODEX_PROMPT.md`
(the sandbox spec), and the *shape* of that prompt is in
`docs/OCR-PROMPT-TEMPLATES.md` (or the marking equivalent).

The reason for this architecture:

- **The Codex sandbox is the safety boundary.** The
  prompt + the wrapper + the disposable-clone + the
  `origin`-removed remote = the only thing keeping the
  real repo from being committed to by an LLM. Keeping
  the prompt construction in Python (where it could be
  done automatically) would make it easier to miss a
  safety rail. The current design forces a human to
  write the prompt file before each run.
- **The prompt is the IP.** When the marking improves,
  it's because the prompt improved, not because the
  model got smarter. Prompts in `_specs/` are versioned
  with the date in the job name; the templates in
  `docs/OCR-PROMPT-TEMPLATES.md` are the canonical
  reference.

## Dependencies

- Python 3.10+ (we use `dt.timezone.utc`, not the 3.11+ `dt.UTC`).
- `pymupdf` for PDF text extraction. Install with `pip install pymupdf`.
- `requests` for Ollama calls.
- `ollama` CLI (only for `ollama pull minimax-m3:cloud`).
- `codex` CLI (for `ocr_batch.py` and `mark_batch.py`).
  Install with the OpenAI Codex CLI installer; auth via
  ChatGPT Pro subscription. The wrapper invokes
  `codex exec --cd <sandbox> --dangerously-bypass-approvals-and-sandbox`.
- `powershell` on the path (for invoking the
  `run_codex_sandbox_job.ps1` wrapper).
