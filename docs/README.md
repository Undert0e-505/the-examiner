# docs/

Long-form documentation that doesn't belong in the README.

- `SETUP.md` — one-time setup. GitHub, Pages, KVdb, secrets, checklist.
- `PRIVACY.md` — privacy and personal-data policy. Why the repo is public despite having personal data in scope, what's allowed in the repo, what isn't, and what to do if a secret is committed by accident.
- `MODEL-CHOICE.md` — why pymupdf for text, `minimax-m3:cloud` for structured extraction, GPT-4o for handwriting.
- `OCR-ACCURACY.md` — what we know about reading the student's handwriting, the lessons from the first end-to-end run, and the calibration loop. (OCR pipeline is built; calibration loop is still aspirational.)
- `OCR-PROMPT-TEMPLATES.md` — the verbatim-rule and graph-specific OCR prompt templates, validated against real data. Copy-paste starting point for the next paper's OCR run.
- `MARKING-PROMPT-TEMPLATE.md` — the per-criterion marking prompt template and the rubric-application rules. Copy-paste starting point for the next paper's marking run.
- `FEEDBACK-PAGE-UX.md` — UX notes for the student-feedback page. **(Built and live, 2026-06-13.)** Read first if you're working on the page.
- `FEEDBACK-PAGE-THUMBS.md` — the per-criterion answer-photo thumbnails, click-to-fullsize lightbox, sizing, and the privacy override that ships the answer photos to the public site. **(Built and live, 2026-06-16.)** Read this before changing the thumb markup or the lightbox behaviour.
- `PIPELINE.md` — the end-to-end pipeline from `index_papers.py` through `publish.py`, with the data flow and the per-stage outputs. **(Reference, valid 2026-06-16.)**
- `REGRESSION-RUN.md` — the benchmark driver (`tests/run_ollama_regression.py`) and the 2026-06-16 qwen3.5:397b-cloud run that produced the regression data. **(Reference, will be revisited on the next paper.)**
