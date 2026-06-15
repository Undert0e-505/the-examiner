================================================================================
CODEX TASK — Redesign the per-assessment page for the-examiner
================================================================================

## What this app is

the-examiner is a GCSE self-marking tool. The student takes a
paper, the system marks their answers against the markscheme, and
publishes a per-assessment HTML page that the student opens to see
the per-criterion breakdown and send feedback per mark. The user
just took a screenshot of the current page and said "this is not a
slick looking web page. I am going to give you one more chance
before we structure this as a codex task." That screenshot is the
acceptance test. The current page looks like a Bootstrap admin
panel, not a product a 14-year-old would be proud to open.

The repo is at D:\dev\the-examiner\ on a Windows box. You are
running inside a disposable copy of the repo at
D:\dev\codex-sandboxes\the-examiner-design-r2\ — do not assume
any other paths exist. Origin remote has been removed; you can
not push, you must not commit unless asked.

## What you are NOT changing

The Python renderer (src/publish.py) and the feedback JS
(src/assets/js/feedback.js) are working and are NOT your
target. Specifically:

  - The data shape produced by the renderer (the
    `render_hero_html`, `render_question_html`,
    `render_criterion_html`, `render_rail_html`,
    `render_index_html` functions) stays as-is. The class
    names they emit (`.hero`, `.qsection`, `.qhead`, `.qbody`,
    `.criterion`, `.criterion-head`, `.verdict-pill`,
    `.feedback`, `.fb-btn`, `.fb-note`, `.rail`, `.rail-card`,
    `.score-block`, `.score-main`, `.score-sub`, `.layout`,
    `.content`, `.batch-list`, `.batch-card`) are the
    contract between Python and CSS. Do not add or remove
    classes. The HTML is the API.
  - The JS file (src/assets/js/feedback.js) stays as-is. It
    reads `<body data-kvdb-bucket="...">`, wires the buttons
    on `.criterion .feedback`, persists to localStorage with
    key `examiner:feedback:<bucket>:<criterion_id>`, and PUTs
    to `https://kvdb.io/<bucket>/student-feedback` on
    `#send-all-btn` click. The DOM structure it expects is
    the contract.
  - The Python entrypoint `D:\Python310\python.exe
    D:\dev\codex-sandboxes\the-examiner-design-r2\src\publish.py
    --batch aqa-84621h-chemistry-higher-2024-05 --yes` must
    still work and must still produce the same HTML. The
    renderer is the data layer; you are writing the visual
    layer. The renderer writes HTML that uses class names;
    you write CSS that styles those class names. The renderer
    also inlines `<script src="assets/js/feedback.js"
    defer></script>` in the per-batch page; that must keep
    working.
  - The privacy boundaries stay. The page must not contain
    the student's full name, email, photos, OCR transcripts,
    or any absolute filesystem paths. The gitignored
    `private/*.json` files (the active identity) drive the
    page's display_name; default is "Aaron" while iterating.
    Do not commit any file inside `private/`, `intake/`,
    `assessments/`, or `papers/`. The published page lives
    at `pages/assessments/aqa-84621h-chemistry-higher-2024-05.html`
    and `pages/index.html` plus `pages/assets/css/styles.css`
    and `pages/assets/js/feedback.js`. The Pages workflow
    uploads the `pages/` directory.

## What you ARE rewriting

ONLY the visual layer:

  - `src/assets/styles.css` — the source of truth for design.
  - `pages/assets/css/styles.css` — the deployed copy, must
    match `src/assets/styles.css` byte-for-byte after each
    rerun of `publish.py`. You can edit `pages/assets/css/styles.css`
    directly in the sandbox; the next re-render of
    `publish.py` will copy it from `src/assets/styles.css` and
    overwrite your change. The right thing is to edit
    `src/assets/styles.css` and then re-run the renderer.

Everything else in the visual surface that you might want to
change (e.g. the per-criterion block markup) — DON'T. The
renderer produces the HTML; if the markup is wrong, fix the
renderer too, but be very clear about what you changed and
why, and do not introduce new top-level structural elements
that the JS doesn't know about.

## The acceptance test

The previous attempt failed for these specific reasons. The
new design must address every one of them. Treat this as the
spec; the prose is the rubric.

1. **The score is the page header.** On a fresh load of the
   per-assessment page on a phone (≤ 480px wide), the
   headline number (e.g. "70/100") is visible above the
   fold without scrolling. The "70%" badge and grade
   descriptor (e.g. "Grade 7+") are visible too. The page
   title text (e.g. "Your aqa 8462 higher result") and the
   meta line (paper code, sitting date, total available)
   may sit below the number; they are secondary. The
   number itself is the page's identity. Think of how a
   Strava activity summary, a Spotify Wrapped card, or a
   Duolingo lesson-completion card presents the number —
   that's the hierarchy you want.

2. **Dark mode contrast is real, not muddy.** The page
   surface is at least 12% lighter than the page background
   (not the current ~7% which makes everything blur
   together). Text on surface passes WCAG AA (4.5:1 for
   body, 3:1 for large text). Verdict pills (AWARD /
   NOT_AWARD / NOT_APPLICABLE) are visually distinct from
   each other and from the surface — not four slightly
   different grays.

3. **Per-criterion verdict is a confident visual statement,
   not a gray pill.** The AWARD verdict is a clear,
   color-coded chip (sage green, NOT muted gray) that takes
   a position. NOT_AWARD is a clear, color-coded chip in
   red. NOT_APPLICABLE is warm grey or amber. The chip is
   big enough to be the dominant visual on its row
   (~12-14px text, ~8px vertical padding, full pill
   border-radius). The awarded / available number (e.g.
   "1/1", "0/2") is set in the display font, not the body
   font, and is the second-largest text on the row.

4. **The feedback controls are a thumb-zone target on
   mobile.** On a 360-414px wide screen, the three
   feedback buttons (Agree / Disagree / I read it as…)
   each take the full row width (one button per row,
   stacked vertically). Each button is at least 48px tall.
   The textarea that appears for Disagree / I read it as
   is at least 96px tall, full width, with visible focus
   state. The "Save feedback" button is a primary button
   (filled, not outlined), full width on mobile, 48px tall.
   On desktop (>= 1024px), the three buttons can be in a
   single row, but each must still be at least 120px wide
   so the text "I read it as…" is not truncated.

5. **The per-question accordion head is a real headline,
   not a chevron-in-void.** When a question is collapsed,
   the user sees: question number (in mono, small) on the
   left, a one-line summary of what the question asked
   (truncated with ellipsis) in the middle, the question's
   total mark in the display font on the right (e.g.
   "8/10" with "8" in accent color, "10" muted), and NO
   big gray chevron taking up the center of the screen.
   The chevron is a 12-16px icon at the far right, or
   could be omitted entirely. The collapsed state should
   be visually quiet, not loud.

6. **The first question is open by default on all
   viewports.** The user should see the first question's
   criteria expanded below the hero, so they don't have to
   tap to discover what's there. Subsequent questions are
   collapsed. The existing JS already does this on
   desktop; make it work on mobile too.

7. **The right rail is the mobile summary card too.** On
   mobile, instead of hiding the rail entirely, show a
   compact "at a glance" card (total, percentage, and the
   "send all" button) ABOVE the per-question list, before
   the first question. The current design hides the rail
   on mobile and loses the at-a-glance summary; that's a
   real product gap.

8. **Spacing is generous.** The per-criterion card has at
   least 20px internal padding on all sides. The gap
   between consecutive criteria is at least 24px. The gap
   between consecutive questions is at least 32px. The
   page has at least 16px horizontal padding on a 360px
   screen, 24px on a 768px screen, 48px on a 1280px
   screen. Lines of body text are at most 70 characters
   wide.

9. **The typography is not generic.** Body text in Inter
   (or a similar humanist sans), numerical values in JetBrains
   Mono (or similar), section headers in Instrument Serif
   (or similar). NO Bricolage Grotesque (the previous
   attempt used it and it read as "tech startup"). NO
   centered everything. NO all-caps section labels. NO
   small-caps. Think: editor's choice serif for the
   number, workhorse sans for body, mono for the
   numerical detail.

10. **The page feels like a product, not a report.** Look
    at how Linear, Vercel, Stripe, Things 3, Bear, or
    Duolingo present feedback. The aesthetic vocabulary is
    "calm confidence" — generous spacing, confident
    typography, a single accent color used sparingly, no
    gradients, no shadows everywhere, no purple/indigo
    unless you actually want that. The page is for a
    14-year-old to look at the score and feel "this
    matters, this is real, I'm going to use this."

## Inputs you have

The sandbox contains a working copy of the repo. Run the
renderer to (re)produce the HTML:

    D:\Python310\python.exe D:\dev\codex-sandboxes\the-examiner-design-r2\src\publish.py --batch aqa-84621h-chemistry-higher-2024-05 --yes

This writes to `pages/assessments/aqa-84621h-chemistry-higher-2024-05.html`
and `pages/index.html` and copies `src/assets/styles.css` to
`pages/assets/css/styles.css`. After the first run, the page
should be inspectable.

You do NOT have an internet browser in the sandbox. You have
file inspection. The HTML output and the CSS source are your
preview. The actual visual rendering happens on a real
device when the user pushes the result and opens the live
URL — your job is to write the CSS such that the produced
HTML, when rendered by a real browser, would meet the
acceptance criteria. Use the rendered HTML to count elements,
check the DOM structure, and validate your CSS selectors.

## What to deliver

In your final response, give me:

  1. The full text of `src/assets/styles.css` (your
     rewrite). This is the load-bearing deliverable.
  2. A short summary of what you changed and why, mapped
     to the 10 acceptance criteria above (one paragraph
     per criterion, or a table).
  3. Any markup changes you had to make in the renderer
     (e.g. adding a `data-*` attribute, adding a class).
     If you made markup changes, also include the
     modified function in `src/publish.py` verbatim. Be
     conservative here — if you can solve it in CSS
     alone, do.
  4. A list of any constraints or tradeoffs you hit, so I
     know what to push back on in review.

## How to iterate

Don't try to produce the perfect design on the first pass.
Write a draft, then re-read the acceptance criteria, then
re-write. If a criterion says "The X must be visible above
the fold on a 360px screen" and your draft has it at 380px
of vertical space, fix the spacing. If a criterion says
"verdict pill is the dominant visual on its row" and your
draft has a 12px pill in muted gray, redesign the row.

Use `git diff` to see what you changed against the starting
state. The diff should be tight: only `src/assets/styles.css`
(plus small markup changes if absolutely necessary). If the
diff touches anything else, justify it.

## Final check before you stop

Open the produced HTML and walk through the page in your head:
fresh load, mobile (360x800), dark mode, expand first
question, click "Disagree" on the first criterion, type a
note, click "Save feedback". Does every step work? Is every
piece of UI in the right place? If not, fix it before you
hand it back.

The user will screenshot the result. Make sure the
screenshot looks like the acceptance criteria, not like
"this is a Bootstrap admin panel."
