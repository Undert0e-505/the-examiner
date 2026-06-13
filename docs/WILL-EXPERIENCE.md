# Will's feedback page — UX notes

**Phase 3+ — not built yet.** This doc describes the design constraints
on the per-assessment HTML page that Will uses to give per-mark
feedback.

## Why the page has to be clean

Will is sitting GCSEs. He is also a teenage boy who has better things
to do than fight a clunky web form. If the page is annoying, he
will not use it, and the calibration loop dies, and the system's
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
identity (one per indexed paper, stable forever). Will's feedback
PUTs to `https://kvdb.io/<paper-bucket>/will-marks` and the polling
script reads from there.

If a bucket is ever compromised (someone finds the URL and writes
garbage), the system re-seeds from `assessments/<batch>/assessor-marks.json`
and treats the existing bucket as untrusted. The bucket id is not
rotated — Will would have to re-submit his feedback otherwise.

## Mobile-first, not desktop-first

Will will open this on his phone, in the kitchen, the night before
the next exam. The page must work on a 6" screen. The buttons must
be thumb-sized. The text field for "I read the writing as X" must
work with a thumb keyboard.

## Why we don't ask for a per-mark confidence score

Confidence scores are for ML pipelines, not for humans. Will either
agrees with a mark or he doesn't. The free-text field captures the
nuance. Asking for a 1-5 score adds noise (Will will pick 3 every
time) and adds clicks.

## When this gets built

The publisher script (`src/publish.py`) is Phase 3+1. It runs
after the assessor writes `assessor-marks.json` and writes a
static HTML file into `pages/assessments/<batch>.html`. The
GitHub Pages deploy workflow is already in the tree
(`.github/workflows/static.yml`); the only thing missing is the
human enabling Pages in the repo Settings (see `docs/SETUP.md` §4).
