# OCR prompt templates

This doc contains the **OCR prompt templates** that have been
validated against real data. The next paper's OCR run is built
from one of these, plus a small list of paper-specific photo
paths and a Codex CLI invocation.

The full operational doc for the OCR run is in
`intake/aqa-84621h-chemistry-higher-2024-05/OCR-RUN.md`. This
doc is **just the prompt templates**, designed to be
copy-pasted.

## Sandbox safety rails (always include)

The first OCR trial on 2026-06-14 BLOCKED for an obvious
reason: the photos are deliberately untracked (AQA copyright),
so `git clone --depth 1` (the wrapper's default mode) excluded
them. The fix was `-UseCopy`, which copies the full working
tree including untracked files. The wrapper at
`D:\dev\openclaw-scripts\codex_lane\run_codex_sandbox_job.ps1`
already has this flag, but the **prompt** also needs the
sandbox safety rails. Every OCR prompt must include:

```
You are operating inside a disposable Codex sandbox at
`<SANDBOX_PATH>`. The real repo is at `<SOURCE_PATH>`. The
sandbox is a fresh copy of the real repo (via `-UseCopy`)
with the `origin` remote removed. **Do not touch the real
repo.** All work happens inside the sandbox.

## Hard rules

- **Do not commit.** Do not push. Do not `git add` anything.
- **Do not run any network command.** No curl, no pip install,
  no git fetch, no `codex login`, nothing.
- **Do not modify any file outside `<list of output files>`.**
- **Do not OCR any photo other than `<list of photos>`.**
- **If you cannot do this task for any reason**, write `BLOCKED`
  at the top of EACH transcript file you have not yet
  completed, and explain in one paragraph why.
```

This is the only thing keeping the OCR pipeline safe. Without
it, Codex would happily commit transcripts to the real repo.

## The verbatim rule (always include)

The single most important rule. Without it, Codex would
"correct" the student's spelling and grammar mistakes in the
transcript, which would break the marking pass downstream. The
rule, applied to every OCR prompt:

```
## What to produce

Each transcript file must contain three sections, in this order:

### 1. Page identification (4-6 lines)
[...]

### 2. Verbatim transcript of Will's handwritten answers

For each answer space on the page, in order, transcribe the
handwriting **verbatim** — every word, every crossed-out word,
every margin scribble, every arithmetic step, every annotation,
every diagram. Conventions:
- For text: write the literal words, including spelling
  mistakes, grammar mistakes, and missing punctuation. If Will
  wrote "Thhe reaction occures when..." write "Thhe reaction
  occures when...". **Do NOT correct to proper English.**
- For chemistry symbols, equations, formulas, or working:
  write them in inline code with backticks, using plain-text
  approximations (e.g. `CaCl2(aq)`, `2Cl- -> Cl2 + 2e-`).
  Preserve the line breaks of the working.
- For numbers and arithmetic: write them exactly as written,
  even if the arithmetic is wrong. If Will wrote `4 * 347`,
  write `4 * 347`, not `2 * 347`.
- For "X" as an unknown: write `X`.
- For ticked MCQ options: write `Ticked: <letter>`.
- For diagrams: [see graph-specific block below]
- For illegible words or numbers: write `[illegible]` and note
  the position. Do not guess.
- For empty answer spaces: write `(no answer written)`.
- For crossed-out words: write the original word followed by
  `(crossed out)`.
- For scribbles / doodles in margins: ignore.

**CRITICAL — DO NOT REWRITE OR CORRECT THE STUDENT'S WORK.**
This applies to prose, arithmetic, and chemistry. The marking
pass is a separate step that compares the transcript to the
markscheme; we need the transcript to be a faithful record of
the page, not a sanitised version.

After the verbatim transcript, add one short sentence in
italics starting `*Reading note:*` describing your confidence.
Be specific about which words or numbers you are sure of and
which you are not.

### 3. Per-page verdict (one sentence)

At the end of each transcript file, after the verbatim section
and the confidence note, add a single line:

`*Verdict:* easy | medium | hard`
```

The verbatim rule held end-to-end in the 2024 AQA Chemistry run.
Codex explicitly flagged in its reading notes that it "found
itself wanting to correct" Will's spelling and grammar mistakes
but left them as written. **This is the rule that keeps the
marking pass defensible.**

## GRAPH-SPECIFIC INSTRUCTION (include for any paper with graph questions)

The most important lesson from the 2024 AQA Chemistry run.
**The first OCR pass silently missed drawn pen marks on a
printed graph** (the two best-fit lines on Q04.1's Figure 3).
Aaron's manual re-check caught this; a second-pass OCR with
this block in the prompt recovered the description.

```
## GRAPH-SPECIFIC INSTRUCTION (most important)

If the page contains a **printed graph** (axes, gridlines,
labels, a Figure box), look at it carefully and describe any
marks the student has added to it. Specifically:

- **Plotted crosses / dots:** report each one with approximate
  coordinates. Use the form `(cross at approximately (x, y))`
  where x and y are read off the gridlines. If you can only
  estimate, write `approximately`.
- **Drawn lines (best-fit, trend, or otherwise):** report each
  line as a description of where it goes, e.g. `(straight line
  drawn from approximately (x1, y1) to approximately (x2, y2))`
  or `(diagonal line rising from near the origin to approximately
  (xmax, ymax))`. If two lines are drawn, describe each
  separately. **Pay special attention to whether the lines
  intersect, and if so, where.**
- **Annotations on the graph (e.g. "X" or arrows or circled
  points):** report these too.
- **A page that has a printed graph with NO drawn marks is a
  real finding, not an OCR error.** If you look carefully at
  the graph and see no crosses, no lines, no annotations,
  write `(no marks on the printed graph)` and move on. But if
  you see ANY pen or pencil marks on the graph, describe them
  in detail. **Do not write "no answer written" for a page
  that has a printed graph without first describing the graph
  area in detail.**
```

This is the difference between a 4-mark under-mark and a clean
transcript on graph-bearing pages. **Always include for any
paper with graph questions; harmless for papers without.**

## Per-file checkpoint instruction (always include for batch OCR)

For a batch OCR run (more than ~3 photos), include the
per-file checkpoint instruction so partial progress survives
interruption:

```
**Write each transcript file IMMEDIATELY after producing it**,
in the listed order. This is important because Codex calls
can be interrupted — partial progress on disk is more useful
than losing all transcripts to a single interruption near
the end.
```

In the 2024 AQA Chemistry full-batch run, Codex wrote the
per-page files incrementally (one every ~30s). The first 7 of
9 Q-NN marking files were on disk when a hypothetical
interruption would have hit at 4 minutes. Without the
checkpoint instruction, all 9 would have been lost.

## Validated template variants

Three template variants have been validated on real data:

1. **`verbatim-only.md`** — the basic OCR prompt with the
   verbatim rule and the per-file checkpoint. Use for a paper
   that has no graph questions. Validated on the 2024 AQA
   Chemistry page 28 ("IDK"), page 26 (bond-energy calc), and
   page 29 (prose) — 3 trials, all clean.
2. **`graph-aware.md`** — the verbatim-only template with the
   GRAPH-SPECIFIC INSTRUCTION block added. Use for any paper
   with graph questions. Validated on the 2024 AQA Chemistry
   pages 6, 10, 11 — recovered the drawn best-fit lines on
   page 10 that the verbatim-only template missed.
3. **`re-pass.md`** — the graph-aware template for a corrective
   second pass after the first pass missed something. Same
   shape, but the prompt explicitly notes which file is being
   re-OCR'd and asks for a fresh transcript that supersedes the
   existing one.

The actual prompt files used in the 2024 AQA Chemistry run are
at `D:\dev\codex-sandboxes\_specs\ocr-batch-*-pages-*\.md` and
`D:\dev\codex-sandboxes\_specs\reocr-graph-pages\04_CODEX_PROMPT.md`.
Those are the source of truth for what the prompts actually
said in the run that produced Will's 70/100 grade.

## Reading notes (the per-page self-assessment)

The verbatim template asks Codex to add a `*Reading note:*`
sentence at the end of each transcript describing its
confidence. This has been valuable in practice — it surfaces
the harder pages and flags them for human review. **The
reading note is a feature, not a noise reduction.** Keep the
rule: every transcript gets a reading note.

The reading note has been particularly useful in three places:

1. **Diagram fidelity:** Codex can describe a printed graph
   well enough to support marking, but a human check on the
   original photo is recommended for diagram-heavy pages. The
   reading note explicitly flags whether the description is
   "detailed enough to mark against."
2. **`[illegible]` honesty:** When Codex can't read a word, it
   writes `[illegible]` and the reading note flags the
   position. The marking pass then knows to either not award
   the criterion (if the illegible word is critical) or apply
   the "process of elimination" check (if the illegible word
   is incidental).
3. **Coefficient and unit errors:** For calculation pages,
   the reading note should specifically call out which
   numbers are confident and which are not. A wrong
   calculation that's been "corrected" in the transcript
   would lose the mark in the marking pass without
   surfacing anywhere; the reading note is where that
   risk is named.

## When to use a 2-pass OCR

The aspirational 2-pass design (Pass 1 literal, Pass 2
re-read with paper + mark scheme in context) is documented in
the older version of `OCR-ACCURACY.md`. **We did not build
it** because the 1-pass Codex OCR was strong enough. If a
future paper's 1-pass transcripts are systematically
insufficient (e.g. the student uses a handwriting style
Codex can't read first time), the 2-pass design is the
fallback. The 2-pass prompt would be:

- **Pass 1 prompt:** the verbatim-only template, asking for
  a literal transcription of the page.
- **Pass 2 prompt:** the re-pass template, but with the
  page's question paper (`paper.json` for this paper) and
  mark scheme (`markscheme.json` for this paper) in the
  context. Codex re-reads the photo, comparing to the rubric,
  and corrects its own misses.

The cost is two GPT-4o (or Codex) calls per photo instead
of one, which doubles the OCR spend. **Don't do this unless
1-pass has failed on a specific page.** The 2024 AQA
Chemistry run was 1-pass, ~11 minutes wall time, ~182k
tokens, ~$0 ChatGPT-Pro spend.
