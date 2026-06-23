"""
src-englit/backends/englit_discover.py — Discovery for English Lit papers.

The englit discovery problem is fundamentally different from chemistry.
In chemistry, photos ARE the exam paper with answers written on them, so
page number → question mapping is deterministic. In English lit:

  - Photos include: cover page, question pages, AND separate answer pages
  - The cover may not be image 1
  - Answer pages have no page numbers, no question references
  - The pipeline must match each answer page to a question by content

This module handles:
  1. Classify each photo as cover / question-page / answer-page
  2. Read the cover to get paper code, total marks, answer requirements
  3. Read question pages to get question prompts
  4. OCR answer pages to get student text
  5. Match each answer page to a question by content similarity

Output: DISCOVERY.json with the shape:
{
  "cover_paper_code": "8702/1H",
  "cover_text": "AQA GCSE English Literature ...",
  "total_marks": 64,
  "questions_to_answer": 4,
  "cover_photo_index": 3,
  "photo_classifications": {
    "1": {"type": "question_page", "question_refs": ["q1"]},
    "2": {"type": "answer_page", "matched_question": "q1", "confidence": "high"},
    "3": {"type": "cover", "question_refs": []},
    ...
  },
  "answered_questions": ["q1", "q3"],
  "notes": "",
  "confidence": "high"
}
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from backends.ollama_m3 import chat_with_images, chat_json_with_images

DEFAULT_MODEL = "qwen3.5:397b-cloud"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 600.0

REPO_ROOT = Path("D:/dev/the-examiner")


# ---------------------------------------------------------------------------
# Step 1: Classify photos and read cover
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM = """You are an OCR/vision assistant for GCSE English Literature
exam papers. You look at photos and classify each one as one of three types:
- "cover": the front cover of the exam paper (has paper code, instructions, marks)
- "question_page": a page from the exam paper showing printed question prompts
- "answer_page": a page with the student's handwritten answers (may be blank
  lined paper or a separate answer booklet)

You respond with JSON only."""

CLASSIFY_USER_TEMPLATE = """You will be shown {n} photos. For EACH photo, classify
it as one of: "cover", "question_page", or "answer_page".

Also identify which photo is the cover page (it may not be the first photo).

For "cover" photos, read the paper code (e.g. "8702/1H"), the total marks
for the paper, and how many questions the student is instructed to answer.

For "question_page" photos, list the question numbers visible on the page
(e.g. ["q1"], ["q1", "q2"]).

For "answer_page" photos, note any visible content that might help match
the answer to a question (e.g. a question number written by the student,
a character name, a poem title, a play name).

## Response shape (JSON only)

{{
  "cover_photo_index": <1-based index of the cover photo>,
  "cover_paper_code": "<paper code, e.g. 8702/1H>",
  "cover_text": "<one-line description>",
  "total_marks": <integer or null if not found>,
  "questions_to_answer": <integer or null if not found>,
  "photo_classifications": {{
    "1": {{
      "type": "cover" | "question_page" | "answer_page",
      "question_refs": [<list of question refs visible, e.g. "q1">],
      "answer_hints": "<any content that hints at which question is being answered>"
    }},
    "2": {{ ... }},
    ...
    "{n}": {{ ... }}
  }},
  "notes": "<anything unusual>",
  "confidence": "high" | "medium" | "low"
}}
"""


def classify_photos(
    photo_paths: list[Path],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Step 1: Classify all photos and read the cover."""
    n = len(photo_paths)
    user_text = CLASSIFY_USER_TEMPLATE.format(n=n)

    print(f"[englit-discover] Classifying {n} photos...", flush=True)
    t0 = time.time()
    result = chat_json_with_images(
        user_text,
        photo_paths,
        system=CLASSIFY_SYSTEM,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )
    elapsed = time.time() - t0
    print(f"[englit-discover] Classification done in {elapsed:.1f}s", flush=True)
    print(f"[englit-discover] Cover: index={result.get('cover_photo_index')}, "
          f"code={result.get('cover_paper_code')}, "
          f"total_marks={result.get('total_marks')}", flush=True)

    return result


# ---------------------------------------------------------------------------
# Step 2: OCR answer pages and match to questions
# ---------------------------------------------------------------------------

MATCH_SYSTEM = """You are an OCR assistant for GCSE English Literature exam
answers. You read photos of handwritten student answers and transcribe them
verbatim. You also determine which exam question each answer is responding to
by matching the content to the question prompts provided.

You respond with JSON only."""

MATCH_USER_TEMPLATE = """You will be shown {n} photos that have been classified
as student answer pages for a GCSE English Literature exam.

## Exam paper info
- Paper code: {paper_code}
- Total marks: {total_marks}
- Questions to answer: {questions_to_answer}

## Question prompts

{question_prompts}

## Your task

For EACH answer photo:
1. Transcribe the student's handwritten answer verbatim (do not correct spelling)
2. Determine which question this answer is responding to by matching the
   content to the question prompts above. Look for:
   - Character names, play titles, poem titles mentioned in the answer
   - Key themes or topics that align with a question prompt
   - Any question number the student may have written
3. Report your confidence in the match: "high", "medium", or "low"

If an answer page cannot be matched to any question, set matched_question to
null and confidence to "low".

## Response shape (JSON only)

{{
  "answers": [
    {{
      "photo_index": <1-based index>,
      "transcript": "<verbatim transcription>",
      "matched_question": "<question ref, e.g. q1, or null>",
      "confidence": "high" | "medium" | "low",
      "match_reason": "<one sentence explaining why you matched this answer to this question>"
    }},
    ...
  ]
}}
"""


def ocr_and_match(
    answer_photo_paths: list[Path],
    answer_photo_indices: list[int],
    paper_code: str,
    total_marks: int | None,
    questions_to_answer: int | None,
    question_prompts: dict[str, str],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Step 2: OCR answer pages and match each to a question.

    question_prompts is a dict mapping question ref (e.g. "q1") to the
    prompt text for that question.
    """
    n = len(answer_photo_paths)
    if n == 0:
        return {"answers": []}

    prompts_text = "\n".join(
        f"### {qref}\n{prompt}\n" for qref, prompt in question_prompts.items()
    )

    user_text = MATCH_USER_TEMPLATE.format(
        n=n,
        paper_code=paper_code,
        total_marks=total_marks or "unknown",
        questions_to_answer=questions_to_answer or "unknown",
        question_prompts=prompts_text,
    )

    print(f"[englit-discover] OCR + matching {n} answer pages...", flush=True)
    t0 = time.time()
    result = chat_json_with_images(
        user_text,
        answer_photo_paths,
        system=MATCH_SYSTEM,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )
    elapsed = time.time() - t0
    print(f"[englit-discover] OCR + matching done in {elapsed:.1f}s", flush=True)

    # Map photo indices back to the original indexing
    answers = result.get("answers", [])
    for i, ans in enumerate(answers):
        if i < len(answer_photo_indices):
            ans["photo_index"] = answer_photo_indices[i]

    return result


# ---------------------------------------------------------------------------
# Step 3: Build DISCOVERY.json
# ---------------------------------------------------------------------------

def build_discovery(
    classification: dict,
    match_result: dict,
    photo_paths: list[Path],
) -> dict:
    """Combine classification + match results into a DISCOVERY.json structure."""

    # Build photo_classifications with matched questions
    photo_classifications = classification.get("photo_classifications", {})
    answers = match_result.get("answers", [])

    # Create a map from photo_index to answer match
    answer_map = {}
    for ans in answers:
        idx = str(ans.get("photo_index", 0))
        answer_map[idx] = ans

    # Merge classification + match into photo_classifications
    for idx_str, cls in photo_classifications.items():
        if cls.get("type") == "answer_page" and idx_str in answer_map:
            ans = answer_map[idx_str]
            cls["matched_question"] = ans.get("matched_question")
            cls["match_confidence"] = ans.get("confidence", "low")
            cls["match_reason"] = ans.get("match_reason", "")

    # Collect answered questions
    answered_questions = []
    for cls in photo_classifications.values():
        mq = cls.get("matched_question")
        if mq and mq not in answered_questions:
            answered_questions.append(mq)

    discovery = {
        "cover_paper_code": classification.get("cover_paper_code", "unknown"),
        "cover_text": classification.get("cover_text", ""),
        "total_marks": classification.get("total_marks"),
        "questions_to_answer": classification.get("questions_to_answer"),
        "cover_photo_index": classification.get("cover_photo_index"),
        "photo_classifications": photo_classifications,
        "answered_questions": answered_questions,
        "notes": classification.get("notes", ""),
        "confidence": classification.get("confidence", "low"),
    }

    return discovery


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def discover_englit(
    photo_paths: list[Path],
    question_prompts: dict[str, str] | None = None,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Full discovery pass for English Lit papers.

    1. Classify all photos (cover / question_page / answer_page)
    2. OCR answer pages and match to questions
    3. Build DISCOVERY.json

    question_prompts: dict of question_ref -> prompt text. If None,
    will be loaded from papers/<slug>/paper.json.

    Returns the DISCOVERY dict and writes it to REPO_ROOT/intake/DISCOVERY.json
    """
    n = len(photo_paths)
    print(f"[englit-discover] Starting englit discovery for {n} photos", flush=True)

    # Step 1: Classify
    classification = classify_photos(
        photo_paths,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )

    # Extract answer page indices
    photo_cls = classification.get("photo_classifications", {})
    answer_indices = []
    answer_paths = []
    for idx_str, cls in photo_cls.items():
        if cls.get("type") == "answer_page":
            idx = int(idx_str) - 1  # 1-based to 0-based
            if 0 <= idx < n:
                answer_indices.append(int(idx_str))
                answer_paths.append(photo_paths[idx])

    print(f"[englit-discover] Found {len(answer_paths)} answer pages, "
          f"{sum(1 for c in photo_cls.values() if c.get('type') == 'question_page')} question pages, "
          f"{sum(1 for c in photo_cls.values() if c.get('type') == 'cover')} cover pages",
          flush=True)

    # Step 2: OCR + match answer pages
    if answer_paths:
        match_result = ocr_and_match(
            answer_paths,
            answer_indices,
            paper_code=classification.get("cover_paper_code", "unknown"),
            total_marks=classification.get("total_marks"),
            questions_to_answer=classification.get("questions_to_answer"),
            question_prompts=question_prompts or {},
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
    else:
        match_result = {"answers": []}

    # Step 3: Build DISCOVERY.json
    discovery = build_discovery(classification, match_result, photo_paths)

    # Write to repo
    out_dir = REPO_ROOT / "intake"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "DISCOVERY.json"
    out_path.write_text(json.dumps(discovery, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[englit-discover] Wrote {out_path}", flush=True)

    return discovery