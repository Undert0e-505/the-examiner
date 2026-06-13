# OCR accuracy and the calibration loop

**Phase 3+ — not built yet.** This doc describes the design we have
in mind for reading Will's handwriting and learning from his
corrections. It is aspirational, written down so the design survives
the gap until we get to it.

## The problem

Will's handwriting is genuinely bad — the school is considering
giving him a laptop for exams. State-of-the-art OCR is required, not
a nice-to-have. The 9-subject scope (maths, English Language,
English Literature, biology, chemistry, physics, history, computer
science, PE) means the handwriting styles are not all the same
(Will's maths scrawl is more legible than his essay scrawl, in our
informal observation).

## The pipeline shape (planned)

1. **Pass 1 — literal transcription.** GPT-4o reads the photo and
   outputs the literal text of Will's answer, no interpretation.
   Confidence is part of the output: words the model is unsure of
   are flagged.
2. **Pass 2 — re-read with context.** GPT-4o sees the literal
   transcription *and* the question paper and mark scheme, and
   re-reads the photo to correct its own misses. This is where
   "is that 'm' or 'rn'?" gets resolved by looking at the mark
   scheme's expected answer.
3. **Marking pass.** GPT-4o takes the Pass 2 transcript, the
   question, and the mark-scheme criteria, and produces
   `assessor-marks.json` with per-criterion decisions and
   justifications.

## Why two OCR passes

A single pass has a single error rate. Two passes, where the second
sees the first, gets the error rate down to roughly the *product*
of the two — and crucially, the second pass has the question +
mark scheme in front of it, so it can resolve ambiguities using
domain context ("if this is a chemistry paper, that word is probably
'sulfate' not 'sulphate'"). The cost is two GPT-4o calls per photo
instead of one, which is acceptable for the accuracy gain.

## Calibration loop

The whole point of the Will-feedback page is the calibration loop:

1. Will marks himself (the primary marker).
2. The examiner (Jimothy / `the-examiner`) marks independently
   (the second pair of eyes).
3. The two compare per mark. Where they agree, the mark stands.
   Where they disagree, Will makes the final call but his
   disagreement is logged.
4. The disagreements are aggregated per subject and written to
   `calibration/<subject>.md` as a running list of "Will values X
   in AO1; the system should follow his lead."
5. The next assessment pass includes the calibration file as
   few-shot examples in the marking prompt.

The calibration file is the *learning artifact*. It's what gets
better over time. It's committed to the repo so the history
survives.

## When this gets built

The OCR pipeline is Phase 3. We have the inputs ready: the
`markscheme.json` from Phase 2 gives the rubric; the `paper.json`
gives the question text. The missing pieces are the photo intake
(Telegram bot to `intake/<batch>/`), the matching step
(`match_paper.py`), and the marking script itself (`assess.py`).
