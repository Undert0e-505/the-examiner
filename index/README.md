# papers.json

The full index of every paper + markscheme in `papers/`. Built by
`src/index_papers.py`.

## Schema

```json
{
  "generated_at": "ISO-8601 timestamp",
  "papers": [
    {
      "id": "aqa-english-lit-2024-jan-1",
      "board": "aqa",
      "subject": "english-lit",
      "series": "jan",
      "year": 2024,
      "paper_number": 1,
      "question_pdf": "papers/aqa-english-lit-2024-jan-1.pdf",
      "markscheme_pdf": "papers/aqa-english-lit-2024-jan-1-ms.pdf",
      "paper_json": "papers/aqa-english-lit-2024-jan-1/paper.json",
      "markscheme_json": "papers/aqa-english-lit-2024-jan-1/markscheme.json",
      "kvdb_bucket": "abc123def456",
      "indexed_at": "ISO-8601 timestamp"
    }
  ]
}
```

The `kvdb_bucket` is generated when the paper is first indexed. It is
stable for the life of the repo. Will's feedback for this paper always
goes to `https://kvdb.io/<kvdb_bucket>/will-marks`.
