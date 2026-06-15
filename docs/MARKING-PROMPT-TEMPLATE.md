# Marking prompt template

This doc contains the **marking prompt template** that has
been validated against real data. The next paper's marking
run is built from this, plus a paper-specific list of
transcript files and a Codex CLI invocation.

The full operational doc for the marking run is in
`assessments/aqa-84621h-chemistry-higher-2024-05/README.md`.
This doc is **just the prompt template** and the
rubric-application rules, designed to be copy-pasted.

## The marking shape (per-question output)

Each Q-NN.marking.md file has three sections:

### 1. Question identification (3-5 lines)

- Question number (e.g. "01", "08")
- Total marks available (sum of `marks` for all criteria in
  this question, from the markscheme)
- The question sub-parts covered by the transcripts (e.g.
  "Q01.1, Q01.2, Q01.3, Q01.4, Q01.5, Q01.6" or "Q08.3
  only")
- A short printed-context summary

### 2. Per-criterion marking (one block per criterion)

For EACH criterion in
`markscheme.json#marks[QN].criteria`, produce a block with
this shape:

```
### Criterion N: <ao> — <marks> mark(s)
**Sub-question this criterion applies to:** <qN.X — your
best guess based on the indicative content and the
transcript>
**Indicative content:** <bullet list from the markscheme>
**Transcript section covered:** <which transcript file and
which question header in it>
**Decision:** AWARD | NOT_AWARD | NOT_APPLICABLE
**Marks awarded:** <integer, 0 to marks available>
**Justification:** <2-4 sentences. Quote the student's
answer where relevant. If NOT_AWARD, explain which line of
the indicative content is missing or contradicted. If
NOT_APPLICABLE, explain why (e.g. "this criterion is for
Q01.X which is on a missing page").>
```

### 3. Question summary (2-3 lines)

- **Total marks awarded for this question:** `<sum of marks
  awarded for all criteria, integer>` out of `<total marks
  for this question>`.
- **One sentence on what cost the most marks.**

## Rubric-application rules

The four rules that make the marking defensible. **All four
must be in the prompt verbatim**, otherwise Codex will slip
into soft marking.

### 1. Verbatim quoting

```
**Important — verbatim quoting:** When you quote the student's
answer, copy it from the transcript file **exactly** —
including spelling mistakes, grammar errors, and arithmetic.
Do not correct or paraphrase it. If the transcript has
`[illegible]`, write `[illegible]` in the justification.
```

Without this rule, the marking pass would quote "the reaction
is exothermic" when the student wrote "the reaction occures"
— and the marking wouldn't be auditable against the photo.

### 2. Process marks with "allow" clauses

```
**Important — process marks:** Many criteria in the
markscheme have "allow" clauses (e.g. "allow correct use of
incorrectly determined values of bonds broken and/or bonds
made", "allow correct evaluation of the expression energy
released = bonds broken − bonds made"). These are explicit.
If a criterion has an "allow" clause, apply it: even if the
student got an earlier step wrong, you can still award the
later step *if the student correctly applied the process to
the wrong value*. Read the transcript line by line and
decide for each criterion whether the student's answer, given
his setup, is internally consistent.
```

In the 2024 AQA Chemistry run, this rule was applied to award
the 1 mark for Q04.2 "where temperature stops rising" (0.77 g
read off the graph, within the markscheme's ±0.05 g
tolerance). It also informed the NOT_AWARD on Q08.3's final
algebra (Will used `7X` not `8X`, and the rubric says "allow
correct use of an incorrectly determined value for 8X" — the
variable name is specific).

### 3. Coefficient and unit errors

```
**Important — coefficient and unit errors:** For calculation
questions, look for the right variables. Q08.3 needs `8X` (8
C-H bonds). If the student wrote `7X` or any other
coefficient, the "8X = 3139" and "X = 392" steps are NOT
awarded because the algebra doesn't follow from the original
setup. The "bonds made" and "bonds broken" process marks may
still be available if the student's setup is internally
consistent. Apply the markscheme strictly.
```

This is the rule that caught Q08.3 — Will knew the *shape* of
the bond-energy calculation, set up the right terms, but used
`7X` instead of `8X`, which cascaded the wrong answer. The
final-answer marks are tied to the `8X` variable, so a
`7X` answer gets 0/4 for the algebra-and-final-answer
process marks even though Will's setup was conceptually
right.

### 4. IDK and empty answers

```
**Important — IDK and empty answers:** Many of Will's answers
are "IDK" / "Idk" / "Not covered yet" / left blank. For these
answers, every criterion for that sub-question is NOT_AWARD
with 0 marks and a one-line justification: "Student wrote
'IDK' / left blank — no content to assess against the
indicative content." Do not speculate about what the student
might have known.
```

In the 2024 AQA Chemistry run, Will wrote "IDK" or "Not
covered yet" or left blank for **5 of the 100 marks** worth
of sub-questions (Q06.2, Q06.3, Q07.6, Q09.3, Q09.4). Each
became a clean NOT_AWARD with a one-line justification, and
the SUM didn't pretend to know what Will *would have* written.

### 5. Crossed-out answers

```
**Important — crossed-out answers:** The transcripts preserve
crossed-out words with `(crossed out)` annotation. A
crossed-out answer is NOT awarded (the student themselves
invalidated it). A new answer written *above or after* the
crossed-out one IS considered.
```

This rule prevented the marking pass from awarding marks for
"Chlorine gas produced (crossed out)" in Q05.2 — Will crossed
out the first answer and wrote "ions can't move" below it.
Only the second answer was eligible.

### 6. Diagram descriptions

```
**Important — diagram descriptions:** Some transcripts
describe diagrams in structured form (e.g. "graph drawn with
plotted points at..."). For markscheme criteria that award
marks for correct graph features (best-fit line, points
plotted, axes labelled), check the diagram description
carefully. If the description says the student drew the line,
award the mark; if it says the student left the graph blank,
do not.
```

This rule was crucial on Q04.1 (the 2 best-fit-line marks).
The graph-specific OCR prompt produced a description detailed
enough to mark against — the marking pass could read the
plotted crosses' approximate coordinates, the lines'
endpoints, and the intersection point, and apply the
markscheme strictly.

### 7. Not-applicable criteria

```
**Important — not-applicable criteria:** Some criteria may
apply to sub-questions whose pages are missing (13, 16, 18)
or that are simply not covered by any of the 26 transcripts.
Mark these as NOT_APPLICABLE with a one-line justification.
```

In the 2024 AQA Chemistry run, the Q8 markscheme had 3
criteria but the page-26 transcript only covered Q8.3 — so
the marking pass marked the Q8.1 prose criterion (3 marks)
and the Q8.2 MCQ criterion (1 mark) as NOT_APPLICABLE, and
applied the Q8.3 calculation criterion (5 marks) as the
only one with a real marking.

## Hard rules (always include)

```
## Hard rules

- **Do not commit.** Do not push. Do not `git add` anything.
- **Do not run any network command.** No curl, no pip install,
  no git fetch, no `codex login`, nothing.
- **Do not modify any file outside the `<N>` output files**
  (`<list of Q-NN.marking.md + SUMMARY.md>`).
- **Do not invent markscheme criteria.** Apply only the
  criteria that exist in
  `markscheme.json#marks[*].criteria[*]`. There are exactly
  `<M>` criteria across 9 questions; do not add more.
- **Do not give the student the benefit of the doubt on the
  basis of "what he probably meant".** If the student's
  algebra uses `7X` and the rubric says `8X`, that is a
  missing mark. Quote the rubric.
- **Do not re-grade the markscheme.** Apply it as written. If
  you think a criterion is unfair, note that in the
  SUMMARY.md pipeline verdict, but do not change the mark.
```

## SUMMARY.md shape

The `SUMMARY.md` file has four sections:

### 1. Paper header (3-4 lines)

- Paper code
- Sitting
- Total marks available
- Total marks awarded

### 2. Per-question tally table (one row per Q)

A markdown table with columns: `Question | Marks available |
Marks awarded | Notes`. One row per Q1-Q9. Notes column is a
one-sentence summary of the question's marking.

### 3. Cross-paper observations (3-5 short paragraphs)

A few short paragraphs (each 2-4 sentences) on cross-cutting
patterns:

- **Calculation vs prose:** Did the student do better on
  prose answers (where they could express a concept in
  their own words) or on calculations (where the markscheme
  has specific numerical answers)? Give specific examples.
- **Specific question types where the OCR was a blocker:**
  List the marks that should be re-checked against the
  original photos by a human.
- **Specific question types where the marking model was
  uncertain:** Marks where the transcript was clear but the
  *marking model* is uncertain (e.g. ambiguous rubric).
- **Pattern of "IDK" answers:** How many sub-questions did
  the student skip or write "IDK" on? Were they clustered in
  any topic?

### 4. Pipeline verdict (3-5 lines)

Three to four sentences:

- Did the OCR + markscheme chain produce marks you would
  defend to a real GCSE marker? (yes / mostly / no)
- What would improve the marking fidelity?
- One sentence on whether this batch pipeline is ready to
  be run on the next paper with minimal changes.

## Per-file checkpoint instruction (always include for batch marking)

For a full-batch marking run (more than ~3 Q-NN files),
include the per-file checkpoint instruction:

```
**Write each Q-NN file IMMEDIATELY after producing it**, in
the listed order (Q01 first, …, Q09 last). The summary file
goes last, after you have all 9 Q-files done.
```

In the 2024 AQA Chemistry full-batch marking run, Codex
wrote the per-question files incrementally (one every ~30s).
The first 7 of 9 Q-NN files were on disk at the 4-minute
mark; without the checkpoint, all 9 would have been lost on
an interruption.

## Validated template variants

Two template variants have been validated on real data:

1. **`single-question.md`** — the per-criterion marking
   prompt for a single question or a small batch (≤3
   questions). Used for the marking trial on Q8 only, before
   the full-batch run. ~7KB prompt, ~92s, ~13k tokens.
2. **`full-batch.md`** — the per-criterion marking prompt
   for all 9 questions in one Codex call. ~13KB prompt,
   ~398s, ~64k tokens, exit 0, 9 per-Q files + 1 SUMMARY
   file.

The actual prompt files used in the 2024 AQA Chemistry run
are at
`D:\dev\codex-sandboxes\_specs\marking-trial-q8\04_CODEX_PROMPT.md`
(single-question) and
`D:\dev\codex-sandboxes\_specs\batch-marking-aqa-2024-05\04_CODEX_PROMPT.md`
(full-batch). Those are the source of truth for what the
prompts actually said in the run that produced Will's 70/100
grade.
