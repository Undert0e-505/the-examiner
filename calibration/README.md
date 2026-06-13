# calibration/

Per-subject learning. One markdown file per subject, updated every time
Will responds to a per-mark prompt.

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
*Will's* approach to each mark, and the kind of evidence he values.
Prepended to the marking prompt as few-shot examples.

Example entry:

```markdown
## English Lit — AO2 (Analysis of language)

- **Will frequently quotes without saying what the quote *does*.** Award
  AO2 only when he names the technique and the effect. Bare quotes
  without effect = no AO2, even if the quote is exactly right.
- **He uses 'juxtaposition' correctly** but calls everything else
  'imagery'. Don't penalise; just say "imagery is a broad category —
  specify which technique."
```

These files are *committed* to the repo (not .gitignored) because they
are the actual learning artifact. They're what gets better over time.
