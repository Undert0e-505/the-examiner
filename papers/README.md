# papers/

GCSE question papers and mark schemes. PDFs are usually dropped
into the Drive-mirrored `exam-papers/` folder (see `docs/SETUP.md`
for the path), and the orchestrator's `papers_sync` step copies
them into this directory on the next run. Filenames are preserved
as-is; the indexer never renames. The content of the cover page
is the source of truth — different awarding bodies use different
filename schemes and we don't try to normalise them.

```
papers/
├── 1ma1-1h-que-20241107.pdf           # Edexcel QP
├── 1ma1-1h-rms-20250109.pdf           # Edexcel MS
├── AQA-84621H-QP-JUN24.PDF            # AQA QP
├── AQA-84621H-MS-JUN24.PDF            # AQA MS
├── AQA-87021-QP-JUN24.PDF
├── AQA-87021-MS-JUN24.PDF
└── <slug>/                            # per-pair, written by index_papers.py
    ├── meta.qp.json
    ├── meta.ms.json
    ├── pair.json
    ├── paper.json                     # written by extract_questions.py (Phase 2)
    ├── markscheme.json                # written by extract_questions.py (Phase 2)
    ├── kvdb-bucket.txt                # UUIDv5, stable forever
    └── raw/                           # per-page text dump, gitignored
```

A real example of a slug directory (after Phase 1 + Phase 2):

```
papers/aqa-84621h-chemistry-higher-2024-05/
├── meta.qp.json
├── meta.ms.json
├── pair.json
├── paper.json            # 44 sub-parts across 9 question groups
├── markscheme.json       # 9 question entries with per-criterion rows
├── kvdb-bucket.txt       # 05c5c944-c9b1-57bb-b313-9cb59673d866
└── raw/
    ├── AQA-84621H-QP-JUN24.txt
    └── AQA-84621H-MS-JUN24.ms.txt
```

## How PDFs get here

The walk-away flow:

1. Drop QP+MS PDFs into
   `D:\AIProjects\Aaron\Jimothy Share\exam-papers\` (the
   Drive-mirrored folder). The folder name is `exam-papers/`
   on this host; it's what the user calls the folder.
2. Send `/mark N` on Telegram. The hook (`~/.openclaw/hooks/
   mark-pipeline-trigger/`) spawns the wrapper.
3. The wrapper calls `src/run.py`, which runs
   `papers_sync.ensure_papers_indexed()` as Step 0/8:
   - Copies any new PDFs from `exam-papers/` → `papers/`
     (filename-dedup — a PDF that's already in `papers/`
     doesn't get re-copied).
   - Runs `src/index_papers.py` to derive slugs from cover
     text, write `meta.{qp,ms}.json` + `pair.json` +
     `kvdb-bucket.txt` + `raw/<basename>.txt`, and update
     `index/papers.json`. Fast, no LLM, ~1-2s.
   - Runs `src/extract_questions.py <slug>` for any slug
     missing `paper.json` or `markscheme.json`. Slow —
     LLM call to Ollama `minimax-m3:cloud`, ~5-10 min
     per slug.
4. Step 0 done. Orchestrator proceeds to Step 1 (auto-discover
   from photos) → Step 2 (markscheme check) → Step 3 (stage +
   OCR via Codex sandbox) → Step 4 (mark via Codex sandbox) →
   Step 5 (publish to `pages/assessments/<slug>.html`) →
   Step 6 (git commit + push) → Step 7 (wait for Pages
   deploy) → Step 8 (email).

The full walk-away flow is described in `README.md` and
`docs/PIPELINE.md`.

## What's committed vs gitignored

| File | Committed? | Why |
|---|---|---|
| `<slug>/meta.{qp,ms}.json` | yes | the indexer's per-file metadata |
| `<slug>/pair.json` | yes | the pair-level summary |
| `<slug>/paper.json` | yes | Phase 2 output, the rubric for the assessor |
| `<slug>/markscheme.json` | yes | Phase 2 output, the rubric for the assessor |
| `<slug>/kvdb-bucket.txt` | yes | the KVdb bucket id (public, not a secret) |
| `<slug>/raw/*.txt` | **no** (gitignored) | rebuildable from the PDF in the same folder |
| The PDFs themselves | yes | the actual source data |

## Slug shape

`<board>-<spec><paper>-<subject>-<tier?>-<YYYY>-<MM>`. Content-derived.
See `src/index_papers.py::build_slug` for the exact rules. Examples:

- `aqa-84621h-chemistry-higher-2024-05` — AQA, spec 8462, paper 1H,
  chemistry, higher tier, exam 2024-05.
- `edexcel-1ma11h-mathematics-higher-2024-11` — Edexcel, spec 1MA1,
  paper 1H, mathematics, higher tier, exam 2024-11.
- `aqa-87021-english-literature-2024-05` — AQA, spec 8702, paper 1
  (no tier for English Lit), exam 2024-05.

## Subjects the indexer knows

In `src/index_papers.py::SPEC_SUBJECT`. To add a new spec, add
an entry to that dict. The indexer will fall back to a text
keyword search on the cover page if the spec is unknown, but
a map entry is preferred (the keyword search is fragile
across OCR variations).

## Recovery

The whole `papers/` directory is rebuildable from the PDFs +
the Drive-mirrored `exam-papers/` folder. To start fresh
(virgin state, no slug dirs, no `index/papers.json`):

1. `papers/*.pdf` and `papers/<slug>/` → Recycle Bin (per
   AGENTS.md standing rule, never `rm`).
2. `index/papers.json` → Recycle Bin.
3. The Drive mirror at `exam-papers/` is the source of
   truth; it stays untouched.

The next `/mark N` re-runs Step 0 and rebuilds everything
in ~5-15 min (depending on how many slugs need
`extract_questions.py`).
