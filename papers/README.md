# papers/

Drop your GCSE question papers and markschemes here. Naming convention:

```
<board>-<subject>-<series>-<year>-<paper>.pdf        # question paper
<board>-<subject>-<series>-<year>-<paper>-ms.pdf    # markscheme
```

Examples:

- `aqa-english-lit-2024-jan-1.pdf` (the question paper)
- `aqa-english-lit-2024-jan-1-ms.pdf` (the markscheme)

The `-ms` suffix is what tells the indexer that this is the markscheme
for a question paper. Without a matching `-ms` PDF, the paper is still
indexed but no markscheme is associated.

After `src/index_papers.py` runs, structured JSON for each paper goes in
a subfolder:

```
papers/aqa-english-lit-2024-jan-1/
├── paper.json          # structured question text
├── markscheme.json     # structured mark criteria
└── kvdb-bucket.txt     # the KVdb bucket id for this paper
```

These subfolders are .gitignored — they're build output. The source of
truth is the PDF.
