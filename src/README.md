# src/

The pipeline scripts. Each step is a small, runnable, reviewable unit.

## Scripts

| Script | Purpose |
|---|---|
| `index_papers.py` | Reads PDFs from `papers/`, OCRs them with Ollama cloud, writes structured JSON. Assigns a KVdb bucket per paper. |
| `intake.py` | Bulk-ingest photos into `intake/<batch>/`. Also handles Telegram single-photo intake. |
| `match_paper.py` | Per-photo paper matching. OCR the photo header / context, fuzzy match against `index/papers.json`. Uncertain photos go to `intake/<batch>/unmatched/`. |
| `ocr_image.py` | Single-image OCR via GPT-4o (with optional pass-2 re-read). |
| `assess.py` | Full marking pipeline for one batch: 2-pass transcription, then marking. Writes `assessments/<batch>/assessor-marks.json` and `narrative.md`. |
| `publish.py` | Generate the per-assessment HTML page for Will. Push to `main` to trigger Pages deploy. |
| `email.py` | Send the rendered email from `outbox/<batch>.txt` to Aaron + Will. |
| `poll_will_feedback.py` | Read Will's KVdb responses, write `corrections.md`, update `calibration/<subject>.md`. |

## Prompts

LLM prompts live in `prompts/` as plain-text files, versioned. They are
the actual IP of this project — when the marking improves, it's because
the prompts improved, not because the model got smarter.

| File | Purpose |
|---|---|
| `transcribe.txt` | Pass 1: literal transcription of a photo. |
| `transcribe_with_context.txt` | Pass 2: re-read with paper + markscheme in context. |
| `mark.txt` | Final marking pass. |
| `calibrate.txt` | Meta-prompt: given Will's recent corrections, what to add to `calibration/<subject>.md`? |
| `<subject>.txt` | Per-subject system context. |
