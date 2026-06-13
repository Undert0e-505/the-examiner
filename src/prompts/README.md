# prompts/

The LLM prompts. Plain text, versioned. They are the actual IP of
this project — when the marking improves, it's because the prompts
improved, not because the model got smarter.

## Convention

Each prompt is paired with a `_meta.txt` that describes:

- What model the prompt is for (Ollama cloud vs GPT-4o)
- What goes in (the user-side content)
- What should come out (the structured output)
- Known failure modes

The prompt file itself uses `{body}` as a placeholder; the caller
substitutes the per-paper raw text in. The prompt file splits at a
named anchor (e.g. `PAPER TEXT:`) into system and user parts; the
extractor's `extract_paper` / `extract_markscheme` do the split.

## Built prompts (Phase 2)

- `extract_qp.txt` + `extract_qp_meta.txt` — QP extraction. See the
  meta for the schema, model, and known failure modes.
- `extract_ms.txt` + `extract_ms_meta.txt` — MS extraction. See the
  meta for the schema, model, and known failure modes.

## Planned prompts (Phase 3+)

- `transcribe.txt` + meta — Pass 1 OCR of a Will photo (GPT-4o).
- `transcribe_with_context.txt` + meta — Pass 2 OCR re-read.
- `mark.txt` + meta — Final marking pass.
- `calibrate.txt` + meta — Meta-prompt for updating
  `calibration/<subject>.md`.
- `<subject>.txt` + meta — Per-subject system context (one per of
  the 9 subjects in scope).

## Versioning

If you change a prompt, commit the old version alongside
(`extract_qp.txt.bak-2026-09-12T18-24`) so the calibration history
makes sense. The extractor reads only `extract_qp.txt` /
`extract_ms.txt`; the `.bak` files are reference for humans.
