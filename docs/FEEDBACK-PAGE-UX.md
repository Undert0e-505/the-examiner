# Feedback page — UX notes

**Phase 3+ — not built yet.** This doc describes the design constraints
on the per-assessment HTML page that the student uses to give per-mark
feedback.

## Why the page has to be clean

The student is sitting GCSEs. They are also a teenager who has better
things to do than fight a clunky web form. If the page is annoying,
they will not use it, and the calibration loop dies, and the system's
whole reason for existing (independent second-marking) is wasted.

## What the page must do

For each mark, three actions:

- **Agree.** One click. PUT `{verdict: "agree"}` to the paper's
  KVdb bucket.
- **Disagree.** One click, then a small text field appears for
  the reason. PUT `{verdict: "disagree", note: "..."}`.
- **I read the writing as X** (free text). Always-visible, never
  hidden behind a click. PUT `{verdict: "free_text", note: "..."}`.

That's it. No scores, no sliders, no "rate your confidence 1-5"
nonsense. The signal we want is binary (agree / disagree) plus a
free-text justification. Anything else is noise.

## Why KVdb, not a database

KVdb is anonymous PUT. No API key, no auth, no rate limits to speak
of, no schema migration to manage. The bucket id is the paper
identity (one per indexed paper, stable forever). The student's
feedback PUTs to `https://kvdb.io/<paper-bucket>/student-marks`
and the polling script reads from there.

If a bucket is ever compromised (someone finds the URL and writes
garbage), the system re-seeds from `assessments/<batch>/assessor-marks.json`
and treats the existing bucket as untrusted. The bucket id is not
rotated — the student would have to re-submit their feedback
otherwise.

## Mobile-first, not desktop-first

The student will open this on their phone, in the kitchen, the
night before the next exam. The page must work on a 6" screen.
The buttons must be thumb-sized. The text field for "I read the
writing as X" must work with a thumb keyboard.

## Why we don't ask for a per-mark confidence score

Confidence scores are for ML pipelines, not for humans. The student
either agrees with a mark or they don't. The free-text field
captures the nuance. Asking for a 1-5 score adds noise (the student
will pick 3 every time) and adds clicks.

## When this gets built

The publisher script (`src/publish.py`) is Phase 3+1. It runs
after the assessor writes `assessor-marks.json` and writes a
static HTML file into `pages/assessments/<batch>.html`. The
GitHub Pages deploy workflow is already in the tree
(`.github/workflows/static.yml`); the only thing missing is the
human enabling Pages in the repo Settings (see `docs/SETUP.md`).
