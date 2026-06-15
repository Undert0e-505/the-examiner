# OCR accuracy and the calibration loop

**Status: built and tested on real data, 2026-06-14.**

The first end-to-end OCR + marking run on the student's 2024 AQA
Chemistry Higher Paper 1 (8462/1H) is in
`intake/aqa-84621h-chemistry-higher-2024-05/` (26 photos +
26 transcripts) and `assessments/aqa-84621h-chemistry-higher-2024-05/`
(9 per-question marking files + summary). The student's grade on
that paper: **70/100**. The OCR + marking pipeline that produced
that result is documented in detail in
`intake/aqa-84621h-chemistry-higher-2024-05/OCR-RUN.md` and
`assessments/aqa-84621h-chemistry-higher-2024-05/README.md`.

This doc captures the **lessons from that run** — the things the
next paper should do differently, or should know going in.

## The pipeline shape (what we actually shipped)

The aspirational doc (the older version of this file) described
a 2-pass OCR: Pass 1 literal, Pass 2 re-read with paper +
mark scheme in context. **We did not build that.** What we
shipped is a **1-pass OCR via Codex CLI in a disposable
sandbox**:

1. Photos arrive at the OpenClaw gateway cache
   (`C:\Users\openclaw-agent\.openclaw\media\inbound\`).
2. `src/ocr_batch.py` (or manual copy) moves them to
   `intake/<paper-slug>/NN.jpg`, named by the printed page
   number on the paper.
3. A Codex CLI call inside a disposable sandbox reads each
   photo and writes a `NN.transcript.md` for each. The
   transcript has three sections: page identification,
   verbatim answer, per-page verdict (`easy` / `medium` /
   `hard`).
4. `src/mark_batch.py` invokes a second Codex call that reads
   each `NN.transcript.md` and the markscheme and writes
   per-question marking files (`Q01.marking.md` through
   `Q09.marking.md`).
5. Per-question marks are summed and a `SUMMARY.md` is
   written. The student's grade: 70/100 for the 2024 AQA
   Chemistry Higher P1.

Why 1-pass instead of 2-pass: the 1-pass Codex OCR was already
strong enough to produce transcripts that the marking pass
could grade against. The 2-pass plan was a "if 1-pass is
insufficient, add this" design — and 1-pass was sufficient, so
2-pass never got built. **If a future paper needs 2-pass** (e.g.
a handwriting style Codex can't read first time), the
`OCR-PROMPT-TEMPLATES.md` doc has a "pass 2" prompt template
that's a strict superset of pass 1.

## The verbatim rule

The single most important design choice. **The OCR pass
transcribes, the marking pass compares to the markscheme.** If
the OCR pass "corrects" the student's work — fixes spelling,
fixes grammar, fixes a wrong calculation to a right one — then
the marking pass would mark the *corrected* transcript against
the markscheme, not the *actual* student work. The whole
calibration loop (see below) depends on the transcript being a
faithful record of what is on the page.

The rule, applied to OCR prompts verbatim:

> **Do not rewrite, paraphrase, or "improve" the student's
> prose, arithmetic, or chemistry. Write what is on the page,
> even if it is rough.** If a word is unclear, write
> `[illegible]`. Do not fill in plausible-looking but
> unverified words. If the student wrote "valance" not
> "valence", write "valance". If the student wrote "electron
> are" (singular, no s), write "electron are". If the student
> wrote `7X` not `8X` in a calculation, write `7X`.

The verbatim rule held end-to-end in the 2024 AQA Chemistry
run. Codex explicitly flagged in its reading notes that it
"found itself wanting to correct" the student's spelling and
grammar mistakes but left them as written.

## The known failure mode: drawn pen marks on printed graphs

The first OCR pass **silently missed drawn pen marks on the
printed graph on page 10** (Q04.1, the two best-fit lines on
Figure 3). The original transcript said `(no answer written)`
on a page where the student had drawn two clearly visible
intersecting best-fit lines. This caused a 4-mark under-mark
on Q04 — Q04.1 0/2 instead of 2/2, Q04.2 graph-interpolation
0/1 instead of 1/1, Q04.2 "no additional reaction" 0/1
instead of 1/1.

**Aaron's manual re-check on 2026-06-14 22:19** caught this
when he looked at the page-10 photo directly. A second-pass
OCR with a graph-specific prompt recovered the description (8
plotted crosses, two best-fit lines, intersection at ~0.77 g)
and the marking was corrected.

**The lesson:** any page with a printed graph or diagram that
the student is expected to draw on **must** use a
graph-specific instruction in the OCR prompt. The default
"describe diagrams in brackets" instruction is right for
*pre-printed* diagrams (e.g. the particle diagram on Q05.1)
but wrong for *student-drawn* marks on printed axes (e.g. the
best-fit lines on Q04.1's Figure 3).

The graph-specific prompt template is in
`docs/OCR-PROMPT-TEMPLATES.md`, in the section titled
"GRAPH-SPECIFIC INSTRUCTION." Copy that block into the
standard OCR prompt for any paper with graph questions. The
re-OCR for the 2024 AQA Chemistry run is at
`D:\dev\codex-sandboxes\_specs\reocr-graph-pages\04_CODEX_PROMPT.md`.

## Other known issues

These came up in the 2024 AQA Chemistry run and are flagged in
the per-Q marking files:

1. **Hallucination risk on calculation pages.** The page-26
   transcript read the first line of the bond-energy working
   as `2x347` for the C-C bond count. The markscheme's correct
   value is 2 (propane has 2 C-C bonds). The student's
   hand-written number is visually ambiguous between `2` and
   `4`. Codex may have transcribed the *correct* value rather
   than the *written* value. Needs a hand-check by Aaron
   against the original photo before publishing the marking.
2. **`[illegible]` fragments.** Pages 03, 07, 09, 22 have
   one or more `[illegible]` annotations in the transcripts.
   The legible surrounding text was enough to make marking
   decisions in all cases, but a final hand-check pass on
   these pages is recommended.
3. **Diagram description fidelity.** Pages with diagrams
   (Q02.4 graph, Q04.1 best-fit lines, Q05.1 particle
   diagram, Q06.2 experimental plan) are described in
   structured form. The description is detailed enough to
   support marking, but a human check on diagram-heavy pages
   is recommended.

## Calibration loop (aspirational — not yet built)

The student-feedback page (`docs/FEEDBACK-PAGE-UX.md`) and the
`poll_student_feedback.py` script that updates
`calibration/<subject>.md` are still in the "planned" column.
The OCR + marking pipeline produces the *input* to that loop
(per-criterion marks, with verbatim transcripts as the
ground-truth record), but the loop itself — the student marks
themselves, the two compare, disagreements are aggregated, the
calibration file is updated — is not implemented.

The first paper (2024 AQA Chemistry) is a good candidate for
the first end-to-end calibration loop test, but that needs
`publish.py` and `email.py` and `poll_student_feedback.py`
to exist first.

## Reusing the OCR pipeline for the next paper

`docs/OCR-PROMPT-TEMPLATES.md` has the full prompt template
with the verbatim rule baked in. The minimum-effort next
paper is:

1. Photos arrive in the gateway cache.
2. `python src/ocr_batch.py <paper-slug> --prompt graph-aware`
   stages them and runs the OCR.
3. `python src/mark_batch.py <paper-slug>` runs the marking.
4. Inspect `assessments/<paper-slug>/SUMMARY.md` for the
   grade, and `*Verdict:* hard` lines in the transcripts for
   pages that need a hand-check.

The `graph-aware` prompt includes the graph-specific block
unconditionally — for a paper without graph questions, the
extra instruction is harmless. For a paper with graph
questions, it's the difference between a 4-mark miss and a
clean transcript.
