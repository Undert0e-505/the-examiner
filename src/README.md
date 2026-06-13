# src/

The pipeline scripts. Each step is a small, runnable, reviewable unit.

## Scripts (built and committed)

| Script | Phase | Purpose |
|---|---|---|
| `index_papers.py` | 1 | Reads PDFs from `papers/`, walks the cover pages with regex (no LLM), writes per-pair metadata + the master `index/papers.json`. Assigns a stable UUIDv5 KVdb bucket per pair. |
| `extract_questions.py` | 2 | Reads the per-page raw text dumps from Phase 1, calls `minimax-m3:cloud` to extract per-question structure into `paper.json` and per-criterion structure into `markscheme.json`. |
| `llm.py` | shared | Minimal Ollama chat wrapper. `chat()` returns text, `chat_json()` requests `format: "json"` and validates against an ad-hoc schema. Retries on 5xx / 429 / connection errors with backoff. |

## Scripts (planned, not built)

| Script | Phase | Purpose |
|---|---|---|
| `intake.py` | 3 | Bulk-ingest photos into `intake/<batch>/`. Also handles Telegram single-photo intake (moves from OpenClaw's gateway cache to `intake/`). |
| `match_paper.py` | 3 | Per-photo paper matching. OCR the photo header / context, fuzzy match against `index/papers.json`. Uncertain photos go to `intake/<batch>/unmatched/`. |
| `ocr_image.py` | 3 | Single-image OCR via GPT-4o. Pass 1 (literal) + pass 2 (re-read with paper + markscheme in context). |
| `assess.py` | 3 | Full marking pipeline for one batch: 2-pass transcription, then marking. Writes `assessments/<batch>/assessor-marks.json` and `narrative.md`. |
| `publish.py` | 3 | Generate the per-assessment HTML page for Will. Push to `main` to trigger Pages deploy (Pages still not enabled in repo settings; see `docs/SETUP.md` §4). |
| `email.py` | 3 | Send the rendered email from `outbox/<batch>.txt` to Aaron + Will. |
| `poll_will_feedback.py` | 3 | Read Will's KVdb responses, write `corrections.md`, update `calibration/<subject>.md`. |

## Prompts (built and committed)

LLM prompts live in `prompts/` as plain-text files, versioned. They are
the actual IP of this project — when the marking improves, it's because
the prompts improved, not because the model got smarter.

| File | Phase | Purpose |
|---|---|---|
| `extract_qp.txt` | 2 | System + user prompt for QP extraction. Returns `{id, paper_question_number, section, extract, prompt, marks_available, page_start, page_end}` per question / sub-part. |
| `extract_ms.txt` | 2 | System + user prompt for MS extraction. Returns `{question_ref, paper_question_number, total_marks_for_question, criteria[]}` per question, where `criteria[].ao` is `AO1` / `AO2` / `AO3` / `AO4` / `null` (Edexcel Maths uses B/M/A process marks per step, not AOs). |

## Prompts (planned, not built)

- `transcribe.txt` — Pass 1 OCR of a Will photo.
- `transcribe_with_context.txt` — Pass 2 OCR re-read.
- `mark.txt` — Final marking pass.
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

## Dependencies

- Python 3.10+ (we use `dt.timezone.utc`, not the 3.11+ `dt.UTC`).
- `pymupdf` for PDF text extraction. Install with `pip install pymupdf`.
- `requests` for Ollama calls.
- `ollama` CLI (only for `ollama pull minimax-m3:cloud`).
