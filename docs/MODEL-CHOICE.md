# Model choice

Why three different models, and which one does what.

| Stage | Model | Why |
|---|---|---|
| PDF text extraction (Phase 1) | **pymupdf text layer** | GCSE papers are typeset. The text layer is reliable. No LLM needed. |
| Structured question + mark-scheme extraction (Phase 2) | **`minimax-m3:cloud`** via Ollama | Long-context (524288 tokens) structured JSON output. Reads the whole paper in one pass, returns verbatim extracts, prompts, AO-tagged criteria, and spec refs. Fast enough for an interactive run (80–180 s per pair, depending on input size). |
| Photo OCR of the student's handwriting (Phase 3, not built yet) | **GPT-4o** via OpenAI API | State-of-the-art on messy handwriting. Cheaper open models lose too much on the student's script, even at 4o-mini. |
| Final marking (Phase 3, not built yet) | **GPT-4o** | The marking pass is where the model needs to be smart, not just fast. The rubric (already in `markscheme.json`) constrains the search space; the model has to apply judgement to the transcript and weigh it against the AO criteria. |

## Why pymupdf is enough for the indexer

GCSE papers and mark schemes are typeset by the awarding bodies. The
text layer is the *source of truth* for the human reading the PDF. We
read the same layer the human reads. No OCR is needed and an LLM
would be wasted on a task a regex can do.

The metadata we extract (board, spec, paper, tier, exam date) is all
in the cover-page text. Pairing QP with MS uses spec+paper+board,
which is unambiguous for a single exam sitting in a single year.

## Why `minimax-m3:cloud` for the extractor

- **Long context.** 524288 tokens, so the whole paper + whole mark
  scheme fit in one call. Per-page chunking would multiply the
  latency without improving quality.
- **Structured JSON output.** Ollama's `format: "json"` constrains
  the response to a parseable shape. We validate against an ad-hoc
  schema after parsing.
- **Vision-capable.** Not used today, but the capability is there
  for a future "scan a cover page, get the metadata" path.
- **Local stub.** `ollama pull minimax-m3:cloud` registers a 362-byte
  proxy that talks to the cloud model upstream. The model itself
  isn't on disk.

**Latency tail.** Long inputs (>30 KB) can take 80–180 s on first
call. Subsequent calls are similar. The script's default
`--timeout 600 --max-retries 1` is tuned for the worst case.

## Why GPT-4o for handwriting and marking

The "decision" parts of the pipeline — reading a messy scrawl,
then applying examiner judgement against the mark scheme — are the
parts where the model has to be smart. `minimax-m3:cloud` is good
enough for the mechanical structured-extraction part of the pipeline,
but for the parts that need reasoning over imperfect inputs, GPT-4o
is the right tool. We accept the API-key-in-a-secret cost as the
trade-off for accuracy on the part that matters most.

The OpenAI API key will live in GitHub Secrets for the workflow
runner and in Windows Credential Manager for local runs (slot
`openai:api-key`, scope = the-examiner only). Not set up yet.
