# papers/

GCSE question papers and mark schemes, dropped in by hand. Filenames
are preserved as-is; the indexer never renames. The content of the
cover page is the source of truth — different awarding bodies use
different filename schemes and we don't try to normalise them.

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

**What's committed vs gitignored:**

| File | Committed? | Why |
|---|---|---|
| `<slug>/meta.{qp,ms}.json` | yes | the indexer's per-file metadata |
| `<slug>/pair.json` | yes | the pair-level summary |
| `<slug>/paper.json` | yes | Phase 2 output, the rubric for the assessor |
| `<slug>/markscheme.json` | yes | Phase 2 output, the rubric for the assessor |
| `<slug>/kvdb-bucket.txt` | yes | the KVdb bucket id (public, not a secret) |
| `<slug>/raw/*.txt` | **no** (gitignored) | rebuildable from the PDF in the same folder |
| The PDFs themselves | yes | the actual source data |

**Slug shape** is content-derived: `<board>-<spec><paper>-<subject>-<tier?>-<YYYY>-<MM>`. See `src/index_papers.py::build_slug`.

**Subjects the indexer knows** are in
`src/index_papers.py::SPEC_SUBJECT`. To add a new spec, add an entry
to that dict. The indexer will fall back to a text keyword search
on the cover page if the spec is unknown, but a map entry is
preferred.
