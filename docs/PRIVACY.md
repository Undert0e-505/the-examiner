# Privacy and personal-data policy

This repo is public on GitHub (a hard requirement for GitHub Pages
on the free tier — see `docs/SETUP.md`). Anything committed to this
repo is therefore publicly visible on the internet. The rules below
exist to keep personal data off the public internet while still
letting the scripts know what name, email, and salutation to use.

## The hard rules

- **Never commit a person's full name, email address, phone number,
  school name, candidate number, date of birth, or any other
  identifying detail about the student the system is built for.**
  The system is built for a single GCSE-age student. Treat that
  student's identity as the highest-value secret in the repo.
- **Never commit a phone photo of handwritten answers.** Photos are
  AQA/Edexcel-copyrighted answer scripts (the awarding body owns
  the paper, the student owns the writing) and they identify the
  student. The `intake/` folder is gitignored for this reason.
- **Never commit a question paper, mark scheme, or extracted text
  of one.** PDFs and raw text-extracts are in
  `papers/<slug>/raw/` and `papers/*.pdf`, both gitignored.
  The awarding body owns the question text.
- **Never commit the OCR transcript of a student's handwritten
  answers.** The transcript quotes the student's writing verbatim
  (spelling mistakes and all) and lives in `intake/<slug>/`, which
  is gitignored. The per-question marking file
  (`assessments/<slug>/QNN.marking.md`) is *not* a verbatim quote
  — it summarises — but it still identifies the student by being
  the marking of *that* student's work. It is gitignored too
  (`assessments/` is gitignored).
- **In the docs that ship in the repo, refer to the student as
  "the student" (or "the student's" / "them").** First-name
  references are off the table for the public repo, even in
  a private repo. The pattern is: describe the role, don't name
  the person.

## Where the real name and email live

They live in `private/will.json` and `private/aaron.json` (both
gitignored), with `private/active.json` as a single-key pointer
that says which one is in use. The scripts (`publish.py`,
`email.py`) read `active.json` first, then load the named
identity file. The flip from staging to live is a one-line
edit to `active.json`. The committed template / docs are in
`private/README.md` (gitignored, but readable for anyone with
access to the box) and the schema is documented there.

The scripts read it with a one-liner like:

```python
import json
from pathlib import Path
student = json.loads(Path("private/student.json").read_text(encoding="utf-8"))
```

If `private/student.json` is missing, the scripts raise a clear
error rather than silently using a placeholder. There is no
default name or email anywhere in the codebase.

## What the docs and code ARE allowed to know

- The student is sitting GCSEs in 2026. (Public knowledge to anyone
  in the household; a year-band is fine.)
- The student has messy handwriting and benefits from GPT-4o-level
  OCR. (Describes the engineering problem, not the person.)
- The student is the second-pair-of-eyes target: they self-mark,
  this system is the independent second pair. (Describes the
  system design.)
- The student has a sibling who is *not* the user of this system
  and is also a minor. (Sibling's name is NOT in this repo, ever.)

## What the docs and code are NOT allowed to know

- First name, last name, full name, nickname, handle, or initials.
- Email address, phone number, school, candidate number, DoB.
- The student's relationship to the repo owner (i.e. that they are
  Aaron's son, or Matthew's brother, or anything similar). The
  relationship is "a student this system serves," not "Aaron's
  child." If a reader can't tell from this repo whether the
  student is Aaron's son, Aaron's neighbour, or a paid client,
  the policy is working.
- The student's exam centre, sitting date, candidate number, or
  any AQA/Edexcel-issued identifier.
- A photo of the student, or a photo of the student's writing
  that has not been redacted. OCR transcripts and the photo
  files are the things most likely to slip through.

## How to check yourself

Before any `git add`, run:

```bash
git diff --cached | grep -iE "will|oakley|@gmail|@yahoo|@outlook|<first-name>|<last-name>"
```

If it returns anything, do not commit. Go back, fix the doc, and
re-run. The check is crude on purpose — false positives (the
modal verb "will", the awarding body's "will endeavour anything"
in the English Literature mark scheme) are safe; false negatives
(the real name slips through) are not.

## What to do if a secret is committed by accident

1. Do NOT just delete the file in a follow-up commit. The history
   still has it.
2. Use `git filter-repo` (preferred) or `git filter-branch` to
   rewrite the history and remove the secret. The rewritten
   history has to be force-pushed, which is normally forbidden
   under `MEMORY.md` standing rules — **this is the one
   exception**. Secrets override the no-force-push rule.
3. If the secret is the student's name or email, also rotate
   the email address. KVdb bucket ids are stable by design, so
   the feedback-page link will continue to work after the
   rotation (the bucket id is in the URL, not the email).
4. Open an incident note in `memory/YYYY-MM-DD.md` describing
   what was leaked, what was done to fix it, and what change
   to the workflow would have caught it.

## Why this is a public repo at all

GitHub Pages on the free tier is hard-gated on repo visibility:
private repos on free personal accounts cannot enable Pages, and
the Pages-enable API returns 422 ("Upgrade or make this repository
public to enable Pages"). The system needs Pages to publish the
per-assessment feedback page. Therefore the repo is public, and
the privacy policy above is the only thing standing between the
student's identity and the public internet.
