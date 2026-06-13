# the-examiner

A second-pair-of-eyes GCSE marker for Will. Photos of his answers come in
via Telegram; this repo ingests the question papers and markschemes, runs
OCR on the photos, matches them to the right paper, marks them, and
publishes the per-question results to GitHub Pages for Will to review.

## Architecture at a glance

```
papers/         PDFs you drop in (questions + markschemes, no script)
intake/         photos from Telegram, one folder per batch
transcripts/    OCR'd photos (markdown)
assessments/    one folder per batch with marks + corrections
calibration/    per-subject feedback memory (built from Will's responses)
pages/          GitHub Pages root
src/            the pipeline scripts
prompts/        LLM prompts (versioned)
outbox/         rendered emails ready to send
```

## How the pipeline runs

1. **You drop PDFs** into `papers/`. Naming convention:
   `<board>-<subject>-<series>-<year>-<paper>.pdf` (e.g. `aqa-english-lit-2024-jan-1.pdf`) and the matching `aqa-english-lit-2024-jan-1-ms.pdf` for the markscheme.
2. **Index runs** — `src/index_papers.py` reads every paper + markscheme PDF in the folder, OCRs them with the Ollama cloud model (typeset text, doesn't need GPT-4o), and produces:
   - `index/papers.json` — a single file listing every indexed paper
   - `papers/<name>/paper.json` — structured question text, per-question
   - `papers/<name>/markscheme.json` — structured mark criteria, per-mark
   - `papers/<name>/kvdb-bucket.txt` — the unique KVdb bucket id for this paper (one bucket per paper; assigned at index time and never changes)
3. **You email a batch of photos** to the Telegram chat. The photos land in `intake/<timestamp>/` (you tell me the batch id, or I infer from when they arrived).
4. **Match runs** — `src/match_paper.py` does a quick OCR on each photo's header / context to figure out which paper it belongs to, then routes each photo to the right assessment.
5. **Assess runs** — `src/assess.py` does a 2-pass transcription (literal pass, then re-read with paper + markscheme in context) and a final marking pass. All three use GPT-4o because handwriting is the hard part.
6. **Publish runs** — `src/publish.py` writes `assessments/<batch>/assessor-marks.json` and a static HTML page, then `git push` triggers GitHub Pages deploy.
7. **Email goes out** — `src/email.py` sends to you and Will with the assessment URL.
8. **Will clicks per mark** — the static page PUTs his responses to the paper's KVdb bucket.
9. **Feedback is harvested** — `src/poll_will_feedback.py` reads Will's responses, writes `corrections.md`, and updates `calibration/<subject>.md`. The next assessment pulls in those calibration notes as few-shot examples.

## Setup (one-time)

See `docs/SETUP.md` (TBD) for: GitHub repo, Pages config, KVdb bucket creation, secrets storage.

## Will's feedback page

`pages/assessments/<batch>.html` (auto-generated). One per mark: agree /
disagree / "I read the writing as X" (free text). PUTs to
`https://kvdb.io/<paper-bucket>/will-marks`.

## Notes on the model choice

- **Paper + markscheme OCR** → Ollama cloud `minimax-m3`. Typeset PDFs, no handwriting, no need for a frontier model.
- **Photo OCR (Will's handwriting)** → GPT-4o via the OpenAI API. State-of-the-art on messy handwriting. The two-pass + Will-feedback loop is the only thing that makes this reliable.
- **Marking** → GPT-4o. The marking pass is where the model needs to be smart, not just fast.
