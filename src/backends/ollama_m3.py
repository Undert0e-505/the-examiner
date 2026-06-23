"""
src/backends/ollama_m3.py ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â Ollama-m3 adapter for the-examiner.

A drop-in alternative to the Codex codex_lane path. Instead of
spinning up a disposable Codex sandbox, we call Ollama's
/api/chat directly with the same prompt body (sans sandbox framing)
and the photos as base64 image attachments.

Public surface:
    chat_with_images(user_text, image_paths, *, system="", ...)
        -> str   (model's text content)
    discover_with_ollama_m3(photo_paths, job_name) -> dict
        Runs the discover task and writes
        D:/dev/codex-sandboxes/<job_name>/intake/DISCOVERY.json
        so the existing read_discovery_json() consumer doesn't need
        to know which backend produced the result.
"""
from __future__ import annotations

import base64
import json
import re
import time
import random
from pathlib import Path
from typing import Any, Iterable

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:397b-cloud"
DEFAULT_TIMEOUT = 600.0  # large: 26 images on a cloud proxy can be slow


class OllamaError(RuntimeError):
    pass


def _b64_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def chat_with_images(
    user_text: str,
    image_paths: Iterable[Path],
    *,
    system: str = "",
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 3,
) -> str:
    """Send a user prompt + N image attachments to Ollama, return the
    assistant's text content. Plain text, no JSON coercion."""
    images = [_b64_image(Path(p)) for p in image_paths]
    url = f"{base_url.rstrip('/')}/api/chat"
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_text, "images": images})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature},
    }
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
        if r.status_code >= 500 or r.status_code == 429:
            last_err = OllamaError(f"{r.status_code}: {r.text[:500]}")
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
        if r.status_code != 200:
            raise OllamaError(f"{r.status_code}: {r.text[:500]}")
        try:
            data = r.json()
        except ValueError as exc:
            raise OllamaError(f"non-JSON response: {r.text[:500]}") from exc
        content = (data.get("message") or {}).get("content")
        if content is None:
            raise OllamaError(f"missing content in response: {data}")
        return content
    raise OllamaError(f"chat_with_images failed after {max_retries} attempts: {last_err}")


def chat_json_with_images(
    user_text: str,
    image_paths: Iterable[Path],
    *,
    system: str = "",
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 3,
) -> Any:
    """Like chat_with_images, but asks for JSON output via Ollama's
    format field. Returns the parsed JSON. Strips code fences if
    the model wrapped its output in ```json ... ```. Retries on
    parse failures with a short backoff."""
    images = [_b64_image(Path(p)) for p in image_paths]
    url = f"{base_url.rstrip('/')}/api/chat"
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_text, "images": images})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature},
        "format": "json",
    }
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
        if r.status_code >= 500 or r.status_code == 429:
            last_err = OllamaError(f"{r.status_code}: {r.text[:500]}")
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
        if r.status_code != 200:
            raise OllamaError(f"{r.status_code}: {r.text[:500]}")
        try:
            data = r.json()
        except ValueError as exc:
            raise OllamaError(f"non-JSON response: {r.text[:500]}") from exc
        content = (data.get("message") or {}).get("content")
        if content is None:
            raise OllamaError(f"missing content in response: {data}")
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            return json.loads(text)
        except ValueError as exc:
            last_err = OllamaError(f"model returned non-JSON: {text[:300]}")
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
    raise OllamaError(f"chat_json_with_images failed after {max_retries} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Discover: produce DISCOVERY.json by calling Ollama with all photos inline.
# Writes to D:/dev/codex-sandboxes/<job>/intake/DISCOVERY.json so the existing
# read_discovery_json() consumer doesn't need to know which backend ran.
# ---------------------------------------------------------------------------

DISCOVERY_SYSTEM = """You are an OCR/vision assistant. You read photos of a
printed GCSE exam paper and identify: (a) the paper code (board + spec + paper
number + tier) and (b) the printed page number on each photo. You are not being
asked to read the student's handwritten answers in this pass ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â that is a
separate, later pass. You MUST respond with valid JSON and nothing else."""


DISCOVERY_USER_TEMPLATE = """You will be shown {n} photos of a GCSE exam paper, in
receipt order. For each photo, you must report the printed page number that is
visible on the page (usually a small number at the top or bottom of the
printed page). For the FIRST photo (the cover), you must also report the
paper code (e.g. "8462/1H") and a one-line human-readable cover description.

The photos are attached inline. You do not need to read the file system ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â
just look at each image.

## Rules

- The keys in `page_numbers` are the 1-based photo index, as strings
  ("1", "2", ..., "{n}"). Values are the printed page number as integers.
- If a page number is not visible on a photo (e.g. cover, or cropped), set
  the value to null for that index.
- `cover_paper_code` is the paper code from the cover (e.g. "8462/1H" or
  "1MA1/1H"). If you cannot read the cover clearly, set it to "unknown".
- `cover_text` is a one-line human-readable description of the cover
  (board + subject + tier + date).
- `question_numbers` is best-effort: a dict from photo index (string) to a
  list of question numbers visible on that photo. Use the paper's own
  notation (e.g. ["Q01.1", "Q01.2"] or ["1.1", "1.2"]). For cover photos,
  use an empty list. Do not over-engineer this ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â best-effort is fine.
- `notes` is a free-form string for anything unusual (smudged page numbers,
  photos that look like duplicates, photos that don't belong, etc.). Empty
  string if nothing to report.
- `confidence` is one of: "high", "medium", "low". Use "low" if the cover
  is unclear or multiple page numbers are illegible.

## Response shape (JSON, no other text)

{{
  "cover_paper_code": "8462/1H",
  "cover_text": "AQA GCSE Chemistry (8462) Higher Tier Paper 1, Friday 17 May 2024",
  "page_numbers": {{
    "1": 1,
    "2": 2,
    ...
    "{n}": {n}
  }},
  "question_numbers": {{
    "1": [],
    "2": ["Q02.1", "Q02.2"],
    ...
  }},
  "notes": "",
  "confidence": "high"
}}

Respond with valid JSON only. No prose before or after the JSON object.
"""


def discover_with_ollama_m3(
    photo_paths: list[Path],
    job_name: str,
    *,
    sandbox_root: Path = Path("D:/dev/the-examiner"),
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Run the discover task on Ollama-m3 and write DISCOVERY.json to
    the sandbox-shaped path. Returns the parsed dict."""
    if not photo_paths:
        raise ValueError("photo_paths must be non-empty")
    user_text = DISCOVERY_USER_TEMPLATE.format(n=len(photo_paths))
    print(
        f"[ollama-m3] discover: sending {len(photo_paths)} photos to "
        f"{model} at {base_url} (timeout {timeout:.0f}s)",
        flush=True,
    )
    t0 = time.time()
    parsed = chat_json_with_images(
        user_text,
        photo_paths,
        system=DISCOVERY_SYSTEM,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )
    elapsed = time.time() - t0
    print(f"[ollama-m3] discover: response in {elapsed:.1f}s", flush=True)
    try:
        print(f"[ollama-m3] discover: cover_paper_code={parsed.get('cover_paper_code')!r}", flush=True)
    except UnicodeEncodeError:
        # Windows console may be cp1252; the data is fine, just the print.
        print("[ollama-m3] discover: cover_paper_code=<unprintable>", flush=True)

    # Sanity-check: must have page_numbers key with at least one entry
    if "page_numbers" not in parsed or not isinstance(parsed["page_numbers"], dict):
        raise OllamaError(
            f"model response missing page_numbers: {json.dumps(parsed)[:300]}"
        )

    # Write to sandbox-shaped path so read_discovery_json() finds it
    out_dir = sandbox_root / "intake"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "DISCOVERY.json"
    out_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ollama-m3] discover: wrote {out_path}", flush=True)
    return parsed
