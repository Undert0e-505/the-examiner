# prompts/

The LLM prompts. Plain text, versioned. They are the actual IP of this
project — when the marking improves, it's because the prompts improved,
not because the model got smarter.

Each prompt is paired with a `_meta.txt` that describes:
- What model the prompt is for (Ollama cloud vs GPT-4o)
- What goes in (the user-side content)
- What should come out (the structured output)
- Known failure modes

If you change a prompt, commit the old version alongside (`transcribe.txt.bak-2026-09-12T18-24`)
so the calibration history makes sense.
