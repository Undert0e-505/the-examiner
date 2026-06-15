"""
src/backends/__init__.py — LLM-backend dispatch for the-examiner.

The pipeline can be driven by Codex (default) or by Ollama-m3 (opt-in).
This package contains the Ollama-m3 adapter; the Codex path stays in the
existing discover_batch.py / ocr_batch.py / mark_batch.py modules.
"""
