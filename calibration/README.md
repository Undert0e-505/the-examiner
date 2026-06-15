# calibration/

**Phase 3+ — not built yet.** The folder is empty by design. This
README describes the design we have in mind for per-subject
calibration.

Per-subject learning. One markdown file per subject, updated every time
the student responds to a per-mark prompt.

```
calibration/
├── english-lit.md
├── maths.md
├── biology.md
├── chemistry.md
├── physics.md
├── history.md
├── computer-science.md
├── pe.md
└── dt.md
```

The content is a running log of patterns Jimothy has learned about
the student's approach to each mark, and the kind of evidence
they value. Prepended to the marking prompt as few-shot examples.

Example entry:

```markdown
## English Lit — AO2 (Analysis of language)

- **The student frequently quotes without saying what the quote *does*.** Award
  AO2 only when the technique and the effect are both named. Bare quotes
  without effect = no AO2, even if the quote is exactly right.
- **'Juxtaposition' is used correctly** but everything else is
  'imagery'. Don't penalise; just say "imagery is a broad category —
  specify which technique."
```

These files are *committed* to the repo (not .gitignored) because
they are the actual learning artifact. They're what gets better
over time.

When Phase 3 is built, the first time a subject is assessed, an
empty `calibration/<subject>.md` is created. As the student
responds to per-mark prompts, `poll_student_feedback.py` appends
learned patterns to the file. Each marking pass prepends the
calibration file to the marking prompt as few-shot examples.
