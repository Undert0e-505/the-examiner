"""
src/llm.py — minimal Ollama chat wrapper for the-examiner.

The pipeline calls a single function, `chat_json`, that:
  - targets http://127.0.0.1:11434 by default (env: OLLAMA_BASE_URL)
  - uses the `minimax-m3:cloud` alias by default (env: OLLAMA_MODEL)
  - requests JSON output (Ollama `format: "json"` first, falls back to
    "JSON please, no other text" in the prompt if the model ignores it)
  - returns a dict (parsed JSON), and validates against a JSON schema
    if one is passed in
  - retries on transient errors (5xx, 429, connection reset) with a
    short backoff

The "JSON schema" passed in is a plain Python dict describing the
expected shape. We don't try to enforce it server-side; we just sanity-
check the keys/types after parsing. The schema is documented in
docs/SCHEMA.md (TBD).
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any

import requests

DEFAULT_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "minimax-m3:cloud")

# Default timeout is generous because the cloud proxy can be slow on first
# call (cold start). Per-call override available.
DEFAULT_TIMEOUT = 180.0


class LLMError(RuntimeError):
    pass


def _build_messages(system: str, user: str) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    return msgs


def chat(
    user: str,
    *,
    system: str = "",
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 3,
) -> str:
    """Return the assistant's text content. Plain text, no JSON coercion.

    Retries on connection / 5xx errors with a short exponential backoff
    plus jitter. Does not retry on 4xx (those are model / prompt bugs)."""
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": _build_messages(system, user),
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
            last_err = LLMError(f"{r.status_code}: {r.text[:500]}")
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
        if r.status_code != 200:
            raise LLMError(f"{r.status_code}: {r.text[:500]}")
        try:
            data = r.json()
        except ValueError as exc:
            raise LLMError(f"non-JSON response: {r.text[:500]}") from exc
        msg = data.get("message") or {}
        content = msg.get("content")
        if content is None:
            raise LLMError(f"missing content in response: {data}")
        return content
    raise LLMError(f"chat failed after {max_retries} attempts: {last_err}")


def chat_json(
    user: str,
    *,
    system: str = "",
    schema: dict[str, Any] | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.0,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = 3,
) -> Any:
    """Return parsed JSON. Asks for JSON via Ollama's format field first;
    if the result isn't valid JSON, falls back to a second pass that
    nudges the model in plain text.

    If `schema` is provided, the parsed result is validated against the
    keys/types the schema describes. `schema` is a tiny ad-hoc format:

      {"type": "object", "required": [...], "properties": {"k": {"type": "..."}}}

    Supported types: "object", "array", "string", "integer", "number",
    "boolean", "null". Anything else is treated as "any".
    """
    url = f"{base_url.rstrip('/')}/api/chat"
    base_payload = {
        "model": model,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature},
    }
    messages = _build_messages(system, user)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        # First attempt: ask for JSON via format. Subsequent attempts: same.
        payload = dict(base_payload)
        payload["messages"] = messages
        payload["format"] = "json"
        try:
            r = requests.post(url, json=payload, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
        if r.status_code >= 500 or r.status_code == 429:
            last_err = LLMError(f"{r.status_code}: {r.text[:500]}")
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
        if r.status_code != 200:
            raise LLMError(f"{r.status_code}: {r.text[:500]}")
        try:
            data = r.json()
        except ValueError as exc:
            raise LLMError(f"non-JSON response: {r.text[:500]}") from exc
        content = (data.get("message") or {}).get("content")
        if content is None:
            raise LLMError(f"missing content in response: {data}")
        text = content.strip()
        # Strip code fences if the model wrapped its JSON in ```json ... ```
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            last_err = LLMError(f"model returned non-JSON: {text[:300]}")
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
            continue
        if schema is not None:
            try:
                _validate(parsed, schema)
            except SchemaError as exc:
                last_err = exc
                time.sleep(0.5 * (2 ** attempt) + random.random() * 0.2)
                continue
        return parsed
    raise LLMError(f"chat_json failed after {max_retries} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Tiny ad-hoc JSON-schema validator. Not a real JSON Schema impl. Just
# enough to catch "model returned the wrong shape" early and retry.
# ---------------------------------------------------------------------------
import re  # noqa: E402  (placed here to keep the public api at the top)


class SchemaError(ValueError):
    pass


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _matches_type(value: Any, expected: str) -> bool:
    py = _TYPE_MAP.get(expected)
    if py is None:
        return True  # unknown type -> skip
    if expected == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, py)


def _validate(value: Any, schema: dict[str, Any]) -> None:
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, t) for t in types):
            raise SchemaError(
                f"expected {'|'.join(types)}, got {type(value).__name__}"
            )
    if expected == "object" and isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                raise SchemaError(f"missing required key: {req!r}")
        props = schema.get("properties", {})
        for k, sub in props.items():
            if k in value:
                _validate(value[k], sub)
    if expected == "array" and isinstance(value, list):
        items = schema.get("items")
        if items is not None:
            for i, v in enumerate(value):
                _validate(v, items)
