# /mark Hook Implementation

## Overview

The `/mark` command is a **gateway-level internal hook**, not an
agent-side behaviour. When a Telegram message starting with `/mark`
arrives, the gateway intercepts it *before* it reaches the agent
session and spawns the pipeline directly. The agent never sees the
`/mark` message as a normal user turn.

## Component map

There are three pieces, each in a different location:

### 1. Hook handler (TypeScript)

**Path:** `~/.openclaw/hooks/mark-pipeline-trigger/handler.ts`

This is the entry point. The gateway loads it as an internal hook
(registered in `openclaw.json` under
`hooks.internal.entries."mark-pipeline-trigger"`). It listens for
`message:received` events, checks if the text starts with `/mark`,
parses the count hint, and spawns the wrapper script.

**Config in `openclaw.json`:**
```json
"hooks": {
  "internal": {
    "enabled": true,
    "entries": {
      "mark-pipeline-trigger": { "enabled": true }
    }
  }
}
```

The hook itself keeps **no state**. It is a thin pass-through: parse
the text, build the CLI args, fire-and-forget the wrapper, push a
confirmation message into `event.messages`.

### 2. Wrapper script (CMD batch)

**Path:** `D:\dev\openclaw-scripts\run-pipeline-with-log.cmd`

Called by the hook handler. This script:
- Validates the repo root exists and has the expected `run.py`
- Creates a timestamped log file under `D:\dev\the-examiner\logs\`
- Invokes `D:\Python310\python.exe <src-folder>\run.py <args>` with
  stdout+stderr captured to the log
- Exits with the same exit code as `run.py`

The wrapper is the thing that actually runs the pipeline. The hook
handler just spawns it and returns immediately.

### 3. Pipeline orchestrator (Python)

**Path:** `D:\dev\the-examiner\src-chemistry\run.py` (and future
`src-englit\run.py`, etc.)

The orchestrator. Does all the real work: papers sync, photo
discovery, OCR, marking, publish, git push, email. See the repo
README and inline docs for the pipeline stages.

## Trigger syntax

```
/mark 26 chemistry   ->  run-pipeline-with-log.cmd --auto-discover --photos-hint 26 --paper-type chemistry --to staging --yes
/mark 20 englit      ->  run-pipeline-with-log.cmd --auto-discover --photos-hint 20 --paper-type englit --to staging --yes
```

The first token after `/mark` is the photo count hint (integer).
The second token is the paper type (e.g. `chemistry`, `englit`).
The paper type determines which `src-<type>\run.py` the wrapper
invokes.

If the paper type is omitted, the default is `chemistry` (for
backwards compatibility).

## How to add a new paper type

1. **Copy an existing `src-<type>/` folder** (e.g.
   `cp -r src-chemistry src-englit`). Each paper type gets its own
   folder with its own `run.py`, `backends/`, prompts, etc.

2. **Update `run-pipeline-with-log.cmd`** to map the new paper type
   name to the right `src-<type>\run.py` path. This is a simple
   lookup in the wrapper.

3. **No hook handler changes needed** — the handler just passes
   `--paper-type <type>` to the wrapper. The wrapper does the
   routing.

4. **No `openclaw.json` changes needed** — the hook is already
   enabled. New paper types are handled by the wrapper, not the
   hook config.

## Files to touch when adding a paper type

| File | Change |
|------|--------|
| `D:\dev\openclaw-scripts\run-pipeline-with-log.cmd` | Add paper-type → src-folder mapping |
| `D:\dev\the-examiner\src-<new-type>\` | Copy from existing type, iterate |

## Files that stay shared across all paper types

| Path | Purpose |
|------|---------|
| `D:\dev\the-examiner\papers\` | Markschemes, indexed by slug |
| `D:\dev\the-examiner\assessments\` | Marking output, per slug |
| `D:\dev\the-examiner\pages\` | Published HTML, per slug |
| `D:\dev\the-examiner\intake\` | Photos and transcripts, per slug |
| `D:\dev\the-examiner\logs\` | Pipeline run logs |
| `~/.openclaw/hooks/mark-pipeline-trigger/handler.ts` | Hook handler (unchanged) |
| `~/.openclaw/openclaw.json` | Hook config (unchanged) |