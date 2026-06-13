# Setup

One-time setup steps for `the-examiner`. Each section is a gate; do them in
order. Re-run this doc when adding a new machine / a new contributor.

## 1. GitHub

The repo is `Undert0e-505/the-examiner` (private).

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

Drop PDFs into `papers/` with the naming convention from the README:

```
<aqa|ocr|eduqas|wjec>-<subject>-<series>-<year>-<paper>.pdf
<aqa|ocr|eduqas|wjec>-<subject>-<series>-<year>-<paper>-ms.pdf
```

Example: `aqa-english-lit-2024-jan-1.pdf` + `aqa-english-lit-2024-jan-1-ms.pdf`.

The `src/index_papers.py` step (when written) reads the folder, OCRs
with the Ollama cloud model, and writes:

- `index/papers.json` (master list)
- `papers/<name>/paper.json` (structured questions)
- `papers/<name>/markscheme.json` (structured mark criteria)
- `papers/<name>/kvdb-bucket.txt` (unique KVdb bucket id, **assigned once
  at index time, never changes**)

## 3. KVdb.io

One bucket per indexed paper. The bucket id is the URL path component
you'll find in `papers/<name>/kvdb-bucket.txt`. The pipeline writes Will's
per-mark responses to `https://kvdb.io/<bucket>/will-marks` as a JSON
array of `{question_id, mark_id, verdict, note}` objects.

- KVdb API: PUT a JSON body to set, GET to read, DELETE to wipe.
- The bucket ids are stable; we do not rotate them. If a bucket is
  ever compromised, treat all responses in it as untrusted and re-seed
  from `assessments/<batch>/assessor-marks.json`.

## 4. GitHub Pages  ⚠️ DEFERRED

**Current state (2026-06-13): workflow is in the tree, Pages is not
enabled.** The deploy workflow `.github/workflows/static.yml` is the
canonical Pages-starter workflow (checkout, configure-pages,
upload-pages-artifact, deploy-pages) and runs on every push to `main`,
but the run fails at `Setup Pages` with:

```
##[error]Get Pages site failed. ... Error: Not Found -
https://docs.github.com/rest/pages/pages#get-a-apiname-pages-site
```

This is expected: the action's whole purpose is to enable Pages, and
`GITHUB_TOKEN` is **explicitly disallowed** from doing that itself
(security gate). A human must enable Pages in the repo UI once.

**To finish enabling Pages (when we get to it):**

1. Open <https://github.com/Undert0e-505/the-examiner/settings/pages>
2. **Build and deployment → Source → GitHub Actions** → Save.
3. Trigger a re-run of the workflow (push to `main` or
   `workflow_dispatch`). The deploy should go green.

**Note: private repo is not a blocker.** Pages is fully supported on
private repos; it serves the site to a public URL (or, on Enterprise,
to org members only). The only thing private-vs-public changes is who
can see the site, not whether Pages works.

**Alternative path (skipped for now):** switch to
`actions/configure-pages` with `enablement: true` + a non-`GITHUB_TOKEN`
PAT in a repo secret. This makes the workflow enable Pages itself.
Costs: one extra secret to manage; PAT lives in GitHub Secrets
(instead of just Windows Credential Manager). We chose the UI path
because the human-in-the-loop "I, a human, approve this repo serving
content on the public internet" gate is worth one click.

## 5. Will's email

`WillJOakley@gmail.com` (per the read of USER-family state; verify
before first send). Aaron and Will both get the per-assessment email.

## 6. Telegram bot for photo intake

The Telegram bot writes photos to `intake/<timestamp>/` under the
bot's working directory. When we wire this up, the bot's working dir
needs to be the root of this repo (or it needs to know how to route
photos to `intake/`). **Not yet set up** — `intake/` is empty by
design for now; the `src/match_paper.py` step is what reads it.

## 7. Secrets / env vars (future)

If we go the `enablement: true` path for Pages (§4) or wire up
OpenAI (for handwriting OCR), we'll need:

- `PAGES_TOKEN` (fine-grained PAT, repo admin + Pages write) — repo secret
- `OPENAI_API_KEY` — repo secret, scope = the-examiner only
- `KVDB_API_KEY` (if we switch from anonymous PUT to authenticated
  buckets) — repo secret

None of these are set up today.

## 8. First-time checklist

- [ ] Token in Credential Manager with Contents + Workflows R/W
- [ ] `papers/` populated with the first batch (Aaron's `Jimothy
      Share/Will-papers/` drop on 2026-06-13 is the seed)
- [ ] Pages enabled in Settings (deferred — see §4)
- [ ] KVdb buckets created (automatic when `src/index_papers.py` runs)
- [ ] Telegram bot wired to `intake/`
- [ ] Will's email confirmed
- [ ] First end-to-end: photo in → assessment page on Pages → email out
      → Will's per-mark response → `corrections.md` regenerated
