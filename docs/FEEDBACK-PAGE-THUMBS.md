# Feedback page — answer-photo thumbnails

**Status:** Built, live. As of commit `9aea554` (2026-06-16) the
per-assessment feedback page shows a clickable thumbnail of the
student's answer photo for each criterion, sitting between the
examiner's justification and the student's agree/disagree controls.

This doc captures the design decisions, the markup, the CSS, the
JS, and the privacy trade-off. Read this before changing the
thumb markup or the lightbox behaviour.

## Why thumbs at all

The feedback page is a two-sided conversation. The examiner says
"you wrote X, here's why that earned 1/1" and the student decides
whether to agree. The student can only check the examiner's claim
against their own writing if they can see their writing on the
page. Without the thumb, the student is trusting the OCR; with
the thumb, they can see the source.

The win is especially large for the disagree path. "I read it as
'Calcium donates 2 of it's valance electrons'" — the student's
free-text reason is one click easier to write when they can see
their own handwriting next to the field they're typing into.

## Where the thumb sits in the markup

Each criterion block in `pages/assessments/<slug>.html` has the
shape:

```html
<div class="criterion AWARD" data-criterion-id="...">
  <div class="criterion-head"> ... marks / AO / subq / verdict ... </div>
  <div class="criterion-body">
    <p class="justification">You wrote `...` ...</p>
  </div>
  <div class="qthumbs qthumbs-criterion" aria-label="...">
    <a class="qthumb" href=".../originals/NN.jpg"
       data-lightbox="Q0X.Y" data-page="N" data-subq="Q0X.Y">
      <img src=".../thumbs/NN.jpg" alt="Your answer for Q0X.Y, page N" />
      <span class="qthumb-label">Q0X.Y - p.N</span>
    </a>
  </div>
  <div class="feedback"> ... agree / disagree / note controls ... </div>
</div>
```

The thumb goes between `criterion-body` (the examiner's note) and
`feedback` (the student's controls). That's the "compare the
examiner's claim against the source, then decide" loop visualised
in DOM order.

## How the sub-question -> page mapping is built

The published HTML is static — generated once when the publisher
script runs. The mapping from sub-question to intake photo page
is built from the codex OCR transcripts, which carry a
`Question numbers visible: 04.1, 04.2, 04.3, 04.4` line. Parsing
that line gives us a `Q04.1 -> [page 11]` map. See
`tests/regression-2026-06-16-qwen3-5-397b-cloud/build_subq_map.py`
for the code.

The build is **idempotent and reproducible** because the codex OCRs
are deterministic (the OCR runs at temperature 0, so re-running
the OCR produces the same transcripts). If the OCR ever produces
a different transcript for the same photos, the thumb mapping
will shift.

## Sizing — `height: 96px` ≈ 2× the agree button

The agree button is `min-height: 48px` (the existing touch-target
spec). The thumb is `height: 96px`, which is exactly 2× the
button. Width resolves from the photo aspect ratio: phone photos
of A4 paper are portrait (ratio 0.47), so a 96px tall thumb is
~45px wide.

This is the smallest size at which the student's handwriting is
still legible. The 320px-wide "question-level" thumb style (the
default `.qthumbs` rule) is **not** used here — that size dominates
the criterion card on mobile and was rejected in commit `d887cde`.

## Multi-thumb layout — flex row, scroll if needed

A sub-question that maps to multiple intake pages (rare, but
possible — e.g., Q05.5's continuation criteria) gets one thumb
per page. They lay out side-by-side in a horizontal row
(`display: flex; flex-direction: row; flex-wrap: nowrap; gap: 8px`)
and scroll horizontally on viewports that are too narrow to fit
them all (`overflow-x: auto`).

The chemistry paper doesn't currently exercise this path (every
sub-question maps to exactly one page), but the CSS is in place
for future papers.

## No border, no white card, no rounding

Three things were tried and rejected, each with a specific
commit:

- `d887cde` introduced a thumb size with a dashed-border wrapper
  and a white inner card. Aaron's "looks like a card" feedback
  rolled this back in `56b53d7`.
- `56b53d7` kept a 1px hairline border around the photo "so the
  edge of the JPG is still visible against the criterion card."
  Aaron's "I don't want the 1 pixel edge" feedback rolled this
  back in `38060ad`.
- The current style is `border: 0; border-radius: 0; background:
  transparent;` on the thumb and the photo. The photo's own
  content (light paper + handwriting on cream) has enough
  natural contrast against the criterion card's `--surface-3`
  background to define its edge.

The lesson: when in doubt, *don't* add a visual chrome around an
inline image. The image defines its own boundary.

## Lightbox click handler

A click on a thumb opens a fixed-position dark overlay (`.qlightbox`)
with the full-size original (320 DPI scanned page, 606×1280 px)
centred in the viewport. The caption underneath reads
"Full size — Q0X.Y, page N" from the `data-subq` and `data-page`
attributes. Esc and click-outside both close the overlay.

The IIFE is appended to `pages/assets/js/feedback.js`. It uses
`e.preventDefault()` on the click so the browser doesn't open the
JPG in a new tab.

## Privacy — the answer photos are public

`docs/PRIVACY.md` originally said: "Never commit a phone photo of
handwritten answers." This was correct for the `intake/` folder
(where the original photos live, gitignored), and was the rule
that kept the per-question marking files (which summarise the
student's writing) also gitignored in `assessments/`.

The thumbnail feature requires the answer photos to be **served
publicly** (otherwise the thumbs on the live page would 404).
Aaron's explicit override on 2026-06-16:

> "I will check with the student what they're comfortable with when they see
> it. this is very much educational / non commercial use."

So the photos live in `pages/assets/photos/<slug>/{originals,thumbs}/`
and are committed and shipped to the public site. The decision is
captured in a comment block in `.gitignore` so the next maintainer
sees it before re-adding the gitignore entry.

If the student changes their mind, or if the repo ever serves a
different student, the override is a one-line revert (re-add
`pages/assets/photos/` to `.gitignore` and remove the new files).

## Cache-buster query string

Static assets (`styles.css`, `feedback.js`) are served with
`Cache-Control: max-age=600` (10 minutes) by GitHub Pages. On a
deploy, the file content changes but the URL doesn't — so the
browser serves a stale version for up to 10 minutes after the
push, and may serve it for longer if the user has the page open.

The fix is a `?v=<commit-sha>` query string on each asset ref,
e.g. `styles.css?v=38060ad`. The next deploy bumps the SHA, the
URL changes, the browser fetches fresh. Bumping is manual (the
publisher script doesn't know the commit SHA), but a one-line
edit per deploy is a cheap price for a hard refresh guarantee.

## How to add a new paper's thumbs

For a new paper (new `<slug>` under `pages/assessments/`), the
sequence is:

1. Re-run the OCR for the new paper (the codex lane writes the
   transcripts to `intake/<slug>/NN.transcript.md`).
2. Run `build_subq_map.py` against the new transcripts to build
   the per-sub-question page map.
3. Run `generate_thumbs.py` to copy the photos and produce the
   320px-wide thumbnails into `pages/assets/photos/<slug>/`.
4. Re-run `publish.py` to regenerate the per-assessment HTML
   with the new thumb blocks injected.
5. Bump the cache-buster query string on the asset refs.
6. Commit. The Pages deploy fires on push.

Each step is a small one-shot script. None of them is integrated
into the main pipeline yet — that's deliberate while the feature
is still being tuned, but the integration is straightforward
when it's time.
