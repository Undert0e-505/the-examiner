# Setup

One-time setup steps for `the-examiner`. Each section is a gate; do them in
order. Re-run this doc when adding a new machine / a new contributor.

## 1. GitHub

The repo is `Undert0e-505/the-examiner` (public, for Pages).

**Token (local Windows Credential Manager, slot `git:https://github.com`):**

- Fine-grained PAT, store with `D:\dev\openclaw-scripts\store-github-pat.ps1`
- Required repository permissions (fine-grained):
  - **Contents:** Read and write
  - **Workflows:** Read and write  ← gates `git push` of `.github/workflows/*`
  - **Metadata:** Read-only (default)
- **Admin** is not required for the operations in this repo. If we add an
  automated Pages-enable path (see §4 below) we'll need Pages write too.

**First clone:**

```powershell
git clone https://github.com/Undert0e-505/the-examiner.git D:\dev\the-examiner
cd D:\dev\the-examiner
```

## 2. Papers and markschemes

Drop PDFs into `papers/`. **Filenames are preserved as-is; the
content of the cover page is the source of truth, not the filename.**
The indexer never renames anything. Real examples of files that have
been indexed (in `papers/`, June 2024 sittings):

- `1ma1-1h-que-20241107.pdf` + `1ma1-1h-rms-20250109.pdf` (Edexcel 1MA1/1H)
- `AQA-84621H-QP-JUN24.PDF` + `AQA-84621H-MS-JUN24.PDF` (AQA 8462/1H)
- `AQA-87021-QP-JUN24.PDF` + `AQA-87021-MS-JUN24.PDF` (AQA 8702/1)

Different awarding bodies use different filename schemes. AQA uses
`<SPEC>-<TIER>-<KIND>-<SERIES><YY>.PDF` (uppercase, kind = QP / MS).
Pearson Edexcel uses lowercase + dashes. We don't normalise. The
indexer reads the cover page, identifies board / spec / paper / tier
/ exam date, and pairs QP with MS by content.

`src/index_papers.py` reads the folder, walks the cover pages with
regex, and writes per pair:

- `index/papers.json` (master list, see `index/README.md` for the schema)
- `papers/<slug>/meta.qp.json` and `meta.ms.json` (per-file metadata)
- `papers/<slug>/pair.json` (slug, board, spec, paper, KVdb bucket)
- `papers/<slug>/kvdb-bucket.txt` (UUIDv5 bucket id, **assigned once
  at index time, never changes**)

**Slug shape:** `<board>-<spec><paper>-<subject>-<tier?>-<YYYY>-<MM>`.
Example: `aqa-84621h-chemistry-higher-2024-05`.

**Subjects the indexer knows about** are in
`src/index_papers.py::SPEC_SUBJECT`. To add a new spec, add an entry
to that dict. The indexer will fall back to a text keyword search on
the cover page if the spec is unknown, but a map entry is preferred.

## 3. KVdb.io

One bucket per indexed paper. The bucket id is the URL path component
you'll find in `papers/<name>/kvdb-bucket.txt`. The pipeline writes the
student's per-mark responses to
`https://kvdb.io/<bucket>/student-marks` as a JSON array of
`{question_id, mark_id, verdict, note}` objects.

- KVdb API: PUT a JSON body to set, GET to read, DELETE to wipe.
- The bucket ids are stable; we do not rotate them. If a bucket is
  ever compromised, treat all responses in it as untrusted and re-seed
  from `assessments/<batch>/assessor-marks.json`.

## 4. GitHub Pages

**Current state:** Pages is not yet enabled. The deploy workflow
`.github/workflows/static.yml` is in the tree; the only thing
missing is the human enabling Pages in the repo UI.

**To finish enabling Pages (when we get to it):**

1. Open <https://github.com/Undert0e-505/the-examiner/settings/pages>
2. **Build and deployment → Source → GitHub Actions** → Save.
3. Trigger a re-run of the workflow (push to `main` or
   `workflow_dispatch`). The deploy should go green.

The pages content (`pages/assessments/<batch>.html`) is also not
written yet — that's Phase 3's `src/publish.py`. Even after enabling
Pages, the site will be empty until the publisher is built.

**Repo must be public on a free personal account.** The visibility
is a hard gate, not a warning: a private repo on a free personal
account shows "Upgrade or make this repository public to enable
Pages" and the Pages-enable API returns 422. The PDFs are
copyright the awarding bodies (AQA, Pearson Edexcel) and are
gitignored (see `.gitignore`); the repo is made public for
Phase 3's static site, not for the PDFs. The KVdb bucket ids
in `papers/*/kvdb-bucket.txt` are also public, but the bucket
itself is anonymous-PUT and the polling script treats
incoming data as untrusted (see §3). **Personal data** (the
student's name, email, photos, transcripts) is gitignored
alongside the PDFs; see `docs/PRIVACY.md` for the policy.

**Alternative paths (skipped):**
  * Pay for GitHub Enterprise and keep the repo private — works,
    not worth the cost.
  * Self-host the static site (Cloudflare Pages, S3 + CloudFront,
    a Pi on the LAN). More work in Phase 3.
  * Use `actions/configure-pages` with `enablement: true` + a
    non-`GITHUB_TOKEN` PAT in a repo secret. This makes the
    workflow enable Pages itself. Costs: one extra secret to
    manage; PAT lives in GitHub Secrets (instead of just
    Windows Credential Manager).

## 5. The student's email

The student's email address is in `private/student.json` (gitignored).
That file is the single source of truth for the publisher, the emailer,
and the feedback harvester. See `docs/PRIVACY.md` for the full policy.

When Phase 5 (Emailer) is built, both the repo owner and the student
get the per-assessment email.

## 6. Telegram bot for photo intake

The Telegram bot writes photos to `intake/<timestamp>/` under the
bot's working directory. When we wire this up, the bot's working dir
needs to be the root of this repo (or it needs to know how to route
photos to `intake/`). **Not yet set up** — `intake/` is empty by
design for now; the `match_paper.py` step (Phase 3) is what reads it.

The OpenClaw gateway already caches Telegram media at
`C:\Users\openclaw-agent\.openclaw\media\inbound\` (UUIDs with
original extensions). The bot should *move* photos from there into
`intake/<batch>/` rather than maintain a separate path. Not wired
yet.

## 7. Secrets / env vars (current and future)

**No secrets committed today.** The repo has no API keys, no
service tokens, no PATs. The only credential is the GitHub PAT in
Windows Credential Manager, used by `git push`.

**When we add Phase 3 (assessor with GPT-4o), we'll need:**

- `OPENAI_API_KEY` — repo secret for the workflow runner, Windows
  Credential Manager (`openai:api-key` slot) for local runs. Scope:
  the-examiner only.

**If we go the `enablement: true` path for Pages (§4), we'll also need:**

- `PAGES_TOKEN` — fine-grained PAT, repo admin + Pages write — repo
  secret. Skipped for now per §4.

**If we switch from anonymous PUT to authenticated KVdb buckets:**

- `KVDB_API_KEY` — repo secret. Skipped for now; anonymous PUTs
  are sufficient.

## 8. First-time checklist (as of 2026-06-13)

- [x] Token in Credential Manager with Contents + Workflows R/W
- [x] `papers/` populated with the first batch (Aaron's `Jimothy
      Share/gcs-papers/` drop on 2026-06-13 — 6 PDFs across Edexcel
      Maths, AQA Chemistry, AQA English Lit)
- [x] Phase 1 (indexer) written, committed (`b34230d`), verified
      idempotent. Three pairs indexed, three KVdb buckets generated.
- [x] Phase 2 (extractor) written, committed (`97e64f1`), ran on
      all three pairs. All 6 `paper.json` / `markscheme.json` files
      validate against the schema.
- [x] `minimax-m3:cloud` pulled into local Ollama, working as the
      Phase 2 LLM. Run with `--timeout 600 --max-retries 1`.
- [x] The student's email confirmed and stored in `private/student.json`
- [x] PDFs gitignored (2026-06-13). `git rm --cached papers/*.pdf`,
      `papers/*.pdf` in `.gitignore`. PDFs stay on disk; the repo
      stops carrying them.
- [ ] Repo visibility flipped to public (see §4). Required for
      Pages to work on a free personal account. Pending API call
      at end of session 2026-06-13.
- [ ] Pages enabled in Settings (see §4; the publisher script is
      Phase 3)
- [ ] Telegram bot wired to `intake/` (deferred — Phase 3)
- [ ] `OPENAI_API_KEY` in repo secret + Credential Manager
      (deferred — Phase 3)
- [ ] First end-to-end: photo in → assessment page on Pages → email
      out → the student's per-mark response → `corrections.md`
      regenerated (deferred — Phase 3)
