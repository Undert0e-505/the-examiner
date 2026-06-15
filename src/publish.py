"""
src/publish.py - generate the static HTML for the per-assessment
                  pages and the dashboard index.

This is a small, self-contained renderer. It does not talk to any
LLM; it reads the already-marked assessment output (produced by
src/mark_batch.py) and emits static HTML into pages/.

The Pages workflow at .github/workflows/static.yml uploads
`pages/` as the artifact. The output of this script IS what gets
deployed to <https://undert0e-505.github.io/the-examiner/>.

Design goals (per the Phase 3 design in docs/FEEDBACK-PAGE-UX.md):
  - Mobile-first, dark mode via prefers-color-scheme, sleek
    design tokens in src/assets/styles.css.
  - Per-question accordion; AWARD/NOT_AWARD/NOT_APPLICABLE
    verdicts with one-line justification.
  - No photos, no raw OCR transcripts, no personal data on the
    public site. The student name shown is the display_name from
    private/student.json (which is "the student" by default).
  - Per-criterion markscheme content is summarised, not
    copy-pasted. (The full markscheme stays in
    papers/<slug>/markscheme.json, which is not uploaded.)
  - A feedback section at the bottom with a link to the KVdb
    PUT endpoint for per-mark feedback.

Usage:

    D:\\Python310\\python.exe src/publish.py ^        --batch aqa-84621h-chemistry-higher-2024-05 ^        --yes

    # all batches:
    D:\\Python310\\python.exe src/publish.py --all --yes

    # dry run (no files written):
    D:\\Python310\\python.exe src/publish.py --batch <slug> --dry-run

The script does NOT push to git. The workflow deploy is
triggered by a separate `git push` to main. This is the
"trigger publish manually" the user asked for.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import http.client
import json
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent   # src/publish.py -> repo root
ASSESSMENTS = REPO_ROOT / "assessments"
PAPERS = REPO_ROOT / "papers"
PRIVATE = REPO_ROOT / "private"
PAGES = REPO_ROOT / "pages"
PAGES_ASSESSMENTS = PAGES / "assessments"
PAGES_ASSETS = PAGES / "assets"
PAGES_CSS = PAGES_ASSETS / "css" / "styles.css"
SRC_CSS = REPO_ROOT / "src" / "assets" / "styles.css"

DISPLAY_NAME_OVERRIDE = "the student"   # the public name; never a real name


# ---------- Parsing ----------

KVDB_BUCKET_RE = re.compile(r"https://kvdb\.io/([A-Za-z0-9-]+)")


def read_active_identity_name() -> str:
    """Read private/active.json to find which identity is active.
    Returns the name (e.g. 'aaron' or 'student')."""
    path = PRIVATE / "active.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} does not exist. Create it with {{'active': 'aaron'}} "
            f"or {{'active': 'student'}}."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    name = data.get("active")
    if name not in ("aaron", "student"):
        raise ValueError(
            f"{path} has active={name!r}; expected 'aaron' or 'student'."
        )
    return name


def read_student_json(require_recipient: bool = False) -> dict:
    """Read the gitignored source-of-truth file for the active
    identity.

    If require_recipient is True, refuse to run if the staging
    recipient email is still the placeholder. publish.py does not
    need the recipient email; send_email.py does.

    Resolution: read private/active.json to find the active
    identity (aaron or student), then read private/<active>.json.
    """
    active = read_active_identity_name()
    path = PRIVATE / f"{active}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} does not exist. Create it from the schema in "
            f"docs/PRIVACY.md, or set active.json to point at the "
            f"other identity."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if require_recipient:
        staging = data.get("recipient_email_staging", "")
        live = data.get("recipient_email_live", "")
        for field, value in (("recipient_email_staging", staging), ("recipient_email_live", live)):
            if not value or "example.com" in value or "REPLACE" in value.upper():
                raise RuntimeError(
                    f"{path} still has a placeholder {field}={value!r}. "
                    f"Edit the file and replace it with a real email "
                    f"before running email.py."
                )
    for k in ("display_name", "first_name_for_salutation"):
        if k not in data:
            raise KeyError(f"{path} is missing the required field '{k}'.")
    # Annotate the loaded dict with the identity name for logging.
    data["_active_identity"] = active
    return data


def read_paper_metadata(slug: str) -> dict:
    """Read papers/<slug>/pair.json + paper.json for the display
    title, board, spec, sitting date."""
    pair_path = PAPERS / slug / "pair.json"
    paper_path = PAPERS / slug / "paper.json"
    if not pair_path.is_file() or not paper_path.is_file():
        raise FileNotFoundError(
            f"Missing papers/{slug}/pair.json or paper.json. "
            f"Re-run the indexer (src/index_papers.py) and extractor "
            f"(src/extract_questions.py) for this paper."
        )
    pair = json.loads(pair_path.read_text(encoding="utf-8"))
    paper = json.loads(paper_path.read_text(encoding="utf-8"))
    return {
        "slug": slug,
        "board": pair.get("board", ""),
        "spec": pair.get("spec", ""),
        "paper": pair.get("paper", ""),
        "tier": pair.get("tier", "") or "—",
        "exam_ym": pair.get("exam_ym", ""),
        "sitting_date": pair.get("sitting_date") or pair.get("exam_ym", ""),
        "title": f"{pair.get('board', '')} {pair.get('spec', '')} {pair.get('tier') or ''}".replace("  ", " ").strip(),
        "total_marks_available": sum(q.get("marks_available", 0) for q in paper.get("questions", [])),
        "kvdb_bucket": pair.get("kvdb_bucket", ""),
    }


def read_kvdb_bucket_from_paper(slug: str) -> str:
    """Read papers/<slug>/kvdb-bucket.txt (one UUIDv5 per line)."""
    path = PAPERS / slug / "kvdb-bucket.txt"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _kvdb_request(method: str, url: str, data: bytes | None = None, content_type: str | None = None, timeout: int = 10) -> tuple[int, str]:
    """Tiny HTTP helper for the kvdb.io API. Returns (status_code, body).
    No auth (kvdb.io is anonymous for create + PUT). Used by the
    bucket self-healing logic.

    Uses http.client (not urllib.request) because urllib.request's
    import chain pulls in the stdlib `email` module, which would
    shadow our src/send_email.py and cause a circular import.
    (As of 2026-06-15, the script was renamed from email.py to
    send_email.py for exactly this reason.)
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"only https is supported, got {parsed.scheme!r}")
    conn = http.client.HTTPSConnection(parsed.hostname, port=parsed.port or 443, timeout=timeout)
    try:
        path = parsed.path
        if parsed.query:
            path = path + "?" + parsed.query
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if data is not None:
            headers["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        return (resp.status, body)
    finally:
        conn.close()


def smoke_test_bucket(bucket_id: str) -> bool:
    """Return True if the bucket is alive (smoke test), False if 404.

    We PUT a tiny sentinel to https://kvdb.io/<bucket>/_smoke_test and
    treat 200/201/204 as "bucket is alive." 404 means the bucket id
    is invalid (either never existed or was garbage-collected). Any
    other status raises so the orchestrator can decide.

    We use PUT, not GET, because kvdb.io returns 404 for GET on a
    key that's never been written — even in a perfectly alive
    bucket. PUT to a never-written key returns 201 Created, which
    is what we want here.
    """
    if not bucket_id:
        return False
    url = f"https://kvdb.io/{bucket_id}/_smoke_test"
    status, _ = _kvdb_request("PUT", url, data=b"1", content_type="text/plain")
    if status in (200, 201, 204):
        # Clean up the sentinel so we don't pollute the bucket.
        try:
            _kvdb_request("DELETE", url)
        except Exception:
            pass
        return True
    if status == 404:
        return False
    raise RuntimeError(f"Unexpected status {status} smoke-testing kvdb bucket {bucket_id}")


def create_new_bucket(recovery_email: str) -> str:
    """Create a new kvdb.io bucket. Returns the new bucket id.
    The recovery_email is the address that can recover the bucket
    if the secret key is lost; for the-examiner that's Aaron's email
    (per MEMORY.md and the 2026-06-15 rotation).
    """
    if not recovery_email or "@" not in recovery_email:
        raise ValueError(
            f"create_new_bucket needs a real email; got {recovery_email!r}. "
            f"Pass the active identity's recipient_email_staging."
        )
    body = urllib.parse.urlencode({"email": recovery_email}).encode("ascii")
    status, text = _kvdb_request(
        "POST", "https://kvdb.io/", data=body, content_type="application/x-www-form-urlencoded"
    )
    if status != 201:
        raise RuntimeError(
            f"Failed to create kvdb bucket: status={status} body={text!r}"
        )
    bucket_id = text.strip()
    if not bucket_id or "/" in bucket_id or " " in bucket_id:
        raise RuntimeError(
            f"kvdb bucket creation returned an unexpected id: {bucket_id!r}"
        )
    return bucket_id


def write_kvdb_bucket_to_paper(slug: str, bucket_id: str) -> Path:
    """Write a bucket id to papers/<slug>/kvdb-bucket.txt. No trailing
    newline so the file content is just the id (consistent with the
    pre-existing convention)."""
    path = PAPERS / slug / "kvdb-bucket.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bucket_id, encoding="utf-8")
    return path


def ensure_kvdb_bucket(slug: str, recovery_email: str) -> tuple[str, bool]:
    """Make sure a live bucket id exists for the paper. Returns
    (bucket_id, rotated) where rotated=True means we just created
    a new one and updated the file (the previous id was 404 or
    missing).

    Workflow:
      1. Read papers/<slug>/kvdb-bucket.txt
      2. If absent or empty: create new bucket, write to file, return (id, True)
      3. If present: smoke-test the id (GET a key)
      4. If smoke-test returns 404: create new bucket, write to file, return (id, True)
      5. If smoke-test returns 200: return (id, False) — bucket is healthy
    """
    existing = read_kvdb_bucket_from_paper(slug)
    if existing:
        if smoke_test_bucket(existing):
            return (existing, False)
        print(
            f"  kvdb bucket {existing!r} for {slug} is 404; rotating to a new one",
            flush=True,
        )
    else:
        print(
            f"  no kvdb bucket for {slug} yet; creating one",
            flush=True,
        )
    new_id = create_new_bucket(recovery_email)
    write_kvdb_bucket_to_paper(slug, new_id)
    print(f"  new bucket id: {new_id}  (recovery email: {recovery_email})", flush=True)
    return (new_id, True)


def parse_summary(slug: str) -> dict:
    """Parse assessments/<slug>/SUMMARY.md into a structured dict.

    SUMMARY.md is a hand-written or LLM-written markdown file with
    four sections. We only extract the parts we need to render the
    HTML; we don't faithfully reproduce the markdown structure.
    """
    path = ASSESSMENTS / slug / "SUMMARY.md"
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist. Run mark_batch.py first.")
    content = path.read_text(encoding="utf-8")

    # Header — look for "Total marks available: N" and "Total marks awarded: N"
    total_available = None
    total_awarded = None
    paper_code = ""
    sitting = ""
    m = re.search(r"Paper code:\s*([^\n]+)", content)
    if m: paper_code = m.group(1).strip()
    m = re.search(r"Sitting:\s*([^\n]+)", content)
    if m: sitting = m.group(1).strip()
    m = re.search(r"Total marks available:\s*(\d+)", content)
    if m: total_available = int(m.group(1))
    m = re.search(r"Total marks awarded:\s*(\d+)", content)
    if m: total_awarded = int(m.group(1))

    # Per-question tally table — match "| Q1 | 10 | 8 | <notes> |"
    q_rows = []
    for m in re.finditer(
        r"\|\s*(Q\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|",
        content,
    ):
        q_rows.append({
            "q": m.group(1),
            "available": int(m.group(2)),
            "awarded": int(m.group(3)),
            "notes": m.group(4).strip(),
        })

    # Observations — pull the paragraphs from "## Cross-paper observations"
    # (student-facing: strengths, IDK pattern, prose-vs-calculation).
    # The current Codex output writes the H2 header in a few shapes
    # depending on prompt version:
    #   "## Cross-Paper Observations" (capital, hyphenated, no number)
    #   "## Cross-paper observations" (lowercase p, no hyphen, no number)
    #   "## 3. Cross-paper observations" (numbered H2, no hyphen)
    #   "#### 3. Cross-paper observations" (numbered H4, no hyphen)
    # The mark prompt asks for the H4-numbered form. Match any of the
    # four (case-insensitive, optional hyphen, optional number, any
    # trailing annotation like "(3-5 paragraphs, STUDENT-FACING)").
    obs = ""
    obs_pat = re.compile(
        r"^#{2,4}\s+(?:\d+\.\s+)?[Cc]ross[\-\s][Pp]aper\s+[Oo]bservations\b[^\n]*$",
        re.MULTILINE,
    )
    obs_match = obs_pat.search(content)
    if obs_match:
        # Find the next H2+ header after the match.
        after = content[obs_match.end():]
        next_header = re.search(r"^#{2,}\s+", after, re.MULTILINE)
        obs = after[:next_header.start()].strip() if next_header else after.strip()

    # Assessor notes (pipeline meta: OCR blockers, marking uncertainty,
    # pipeline verdict). Rendered as a collapsed dropdown at the bottom
    # of the per-assessment page; never shown to the student. Same
    # # of header shapes as the observations section.
    assessor_notes = ""
    notes_pat = re.compile(
        r"^#{2,4}\s+(?:\d+\.\s+)?Assessor\s+[Nn]otes\b[^\n]*$",
        re.MULTILINE,
    )
    notes_match = notes_pat.search(content)
    if notes_match:
        after = content[notes_match.end():]
        next_header = re.search(r"^#{2,}\s+", after, re.MULTILINE)
        assessor_notes = after[:next_header.start()].strip() if next_header else after.strip()

    # Backwards-compat: legacy SUMMARY.md files used "## 4. Pipeline verdict"
    # without a separate assessor-notes section. If we didn't find an
    # "Assessor notes" header, fall back to the old "Pipeline verdict"
    # so older SUMMARYs still render their pipeline meta into the dropdown.
    if not assessor_notes:
        legacy_pat = re.compile(
            r"^#{2,4}\s+(?:\d+\.\s+)?[Pp]ipeline\s+[Vv]erdict\b[^\n]*$",
            re.MULTILINE,
        )
        legacy_match = legacy_pat.search(content)
        if legacy_match:
            after = content[legacy_match.end():]
            next_header = re.search(r"^#{2,}\s+", after, re.MULTILINE)
            assessor_notes = after[:next_header.start()].strip() if next_header else after.strip()

    return {
        "paper_code": paper_code,
        "sitting": sitting,
        "total_available": total_available,
        "total_awarded": total_awarded,
        "q_rows": q_rows,
        "observations_md": obs,
        "assessor_notes_md": assessor_notes,
    }


def extract_section(text: str, start_marker: str, end_marker: str | None) -> str:
    """Extract the body of a markdown section between two H2 markers."""
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    if end_marker:
        end = text.find(end_marker, start)
        if end < 0: end = len(text)
    else:
        end = len(text)
    return text[start:end].strip()


def parse_question_marking(slug: str, q_label: str) -> dict:
    """Parse assessments/<slug>/Q<N>.marking.md into a structured
    per-criterion dict."""
    path = ASSESSMENTS / slug / f"{q_label}.marking.md"
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8")

    # Question identification
    qnum = q_label.lstrip("Q").lstrip("0")
    qnum = str(int(qnum))   # normalize "01" -> "1"
    qnum = qnum.zfill(2)
    total_avail = None
    m = re.search(r"Total marks available:\s*(\d+)", content)
    if m: total_avail = int(m.group(1))

    subparts_covered = ""
    m = re.search(r"Question sub-parts covered by the transcripts:\s*([^\n]+)", content)
    if m: subparts_covered = m.group(1).strip()

    printed_context = ""
    m = re.search(r"Printed-context summary:\s*([^\n]+)", content)
    if m: printed_context = m.group(1).strip()

    # Per-criterion blocks. Each starts with "### Criterion N: <AO> -- <marks> mark(s)"
    criteria = []
    blocks = re.split(r"### Criterion \d+:\s*", content)
    for block in blocks[1:]:    # skip preamble
        c = parse_criterion_block(block)
        if c: criteria.append(c)

    # Question summary. The current Codex output writes the H2
    # header as "## Question Summary" (no number, capital S); the
    # mark prompt asks for "#### 3. Question summary" (H4,
    # lowercase s, with number). Try the current form first, then
    # the prompt's form for older transcripts.
    q_summary = ""
    m = re.search(r"## Question Summary\s*\n+([^#]+)", content, re.DOTALL)
    if m: q_summary = m.group(1).strip()
    if not q_summary:
        m = re.search(r"## 3\. Question summary\s*\n+([^#]+)", content, re.DOTALL)
        if m: q_summary = m.group(1).strip()
    if not q_summary:
        m = re.search(r"#### 3\. Question summary\s*\n+([^#]+)", content, re.DOTALL)
        if m: q_summary = m.group(1).strip()

    # Legibility assessment. The mark prompt asks for a
    # `### Legibility` block (H3) with four fields: legibility_score
    # (int 0-5), ocr_mode (enum), reason (sentence), student_feedback
    # (sentence). Try the current form first (no number), then the
    # numbered form (older transcripts). Returns a dict with the
    # four fields and a derived `flag_for_recheck` (True if score
    # <= 2 or ocr_mode is context_inferred/unreadable).
    legibility = _parse_legibility_block(content)

    return {
        "qnum": qnum,
        "q_label": q_label,
        "total_available": total_avail or sum(c.get("marks_available", 0) for c in criteria),
        "total_awarded": sum(c.get("marks_awarded", 0) for c in criteria),
        "subparts_covered": subparts_covered,
        "printed_context": printed_context,
        "criteria": criteria,
        "q_summary_md": q_summary,
        "legibility": legibility,
    }


def _parse_legibility_block(content: str) -> dict:
    """Parse the per-question Legibility section. Returns a dict:
        {
          "legibility_score": int | None,    # 0-5, or None if not found
          "ocr_mode": str | None,           # clear_read | minor_uncertainty | context_inferred | unreadable
          "reason": str,                    # one short sentence
          "student_feedback": str,          # one short sentence
          "flag_for_recheck": bool,         # True if score <= 2 or ocr_mode in {context_inferred, unreadable}
        }
    If the section is missing or any required field is empty,
    the field is None / empty / False (the renderer falls back to
    "no legibility data" rather than fabricating).
    """
    # Try un-numbered form first (current Codex output), then
    # numbered form (older transcripts).
    block = None
    for pattern in (
        r"### Legibility\s*\n+(.*?)(?=\n### |\n## |\Z)",  # un-numbered
        r"#### 3\. Legibility.*?\n+(.*?)(?=\n#### |\n### |\n## |\Z)",  # numbered
    ):
        m = re.search(pattern, content, re.DOTALL)
        if m:
            block = m.group(1)
            break
    if not block:
        return {
            "legibility_score": None,
            "ocr_mode": None,
            "reason": "",
            "student_feedback": "",
            "flag_for_recheck": False,
        }

    # Extract each field by its bold-prefix label.
    def _field(label: str) -> str:
        m = re.search(rf"\*\*{re.escape(label)}:\*\*\s*([^\n\r]+)", block)
        return m.group(1).strip() if m else ""

    score_raw = _field("legibility_score")
    ocr_mode = _field("ocr_mode")
    reason = _field("reason")
    student_feedback = _field("student_feedback")

    legibility_score: int | None = None
    if score_raw:
        try:
            legibility_score = int(score_raw)
        except ValueError:
            legibility_score = None

    # Validate ocr_mode against the enum. Codex should be writing
    # one of the four valid values, but we don't crash if it
    # invents something new -- just flag it.
    valid_modes = {"clear_read", "minor_uncertainty", "context_inferred", "unreadable"}
    if ocr_mode and ocr_mode not in valid_modes:
        # Unknown mode. Keep the string so the assessor can see
        # what Codex wrote, but don't claim it's a known value.
        pass

    flag_for_recheck = False
    if legibility_score is not None and legibility_score <= 2:
        flag_for_recheck = True
    if ocr_mode in ("context_inferred", "unreadable"):
        flag_for_recheck = True

    return {
        "legibility_score": legibility_score,
        "ocr_mode": ocr_mode,
        "reason": reason,
        "student_feedback": student_feedback,
        "flag_for_recheck": flag_for_recheck,
    }


def parse_criterion_block(block: str) -> dict | None:
    """Parse a single criterion block from a Q*.marking.md file."""
    # The header is "AO3 -- 2 mark(s)"
    header_m = re.match(r"([^\n]+?)\s*\n", block)
    if not header_m:
        return None
    header = header_m.group(1).strip()
    # Extract AO and marks
        # Extract AO and marks. Tolerate either ASCII "--" or the
    # Unicode em-dash U+2014 between AO and the marks count, AND
    # in the AO group itself; Codex's markdown output drifts
    # between the two and the previous regex silently dropped
    # every criterion when the format changed, which rendered as
    # a generic "No per-criterion details available." on the page.
    ao_m = re.match(r"^(AO\d+|—|-|null|-)\s*(?:-|—)+\s*(\d+)\s*marks?(?:\(s\))?", header)
    if not ao_m:
        return None
    ao = ao_m.group(1)
    marks_available = int(ao_m.group(2))

    # Sub-question
    subq = ""
    m = re.search(r"\*\*Sub-question this criterion applies to:\*\*\s*([^\n]+)", block)
    if m: subq = m.group(1).strip()

    # Indicative content (bullet list)
    indicative = []
    m = re.search(r"\*\*Indicative content:\*\*\s*\n((?:- [^\n]+\n?)+)", block)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if line.startswith("- "):
                indicative.append(line[2:].strip())

    # Transcript section
    transcript_sec = ""
    m = re.search(r"\*\*Transcript section covered:\*\*\s*([^\n]+)", block)
    if m: transcript_sec = m.group(1).strip()

    # Decision
    decision = ""
    m = re.search(r"\*\*Decision:\*\*\s*([A-Z_]+)", block)
    if m: decision = m.group(1).strip()

    # Marks awarded
    marks_awarded = 0
    m = re.search(r"\*\*Marks awarded:\*\*\s*(\d+)", block)
    if m: marks_awarded = int(m.group(1))

    # Justification
    justification = ""
    m = re.search(r"\*\*Justification:\*\*\s*([^\n]+(?:\n(?!\*\*)[^\n]+)*)", block)
    if m: justification = m.group(1).strip()

    return {
        "ao": ao,
        "marks_available": marks_available,
        "subq": subq,
        "indicative": indicative,
        "transcript_sec": transcript_sec,
        "decision": decision,
        "marks_awarded": marks_awarded,
        "justification": justification,
    }


# ---------- HTML rendering ----------

def esc(s: str) -> str:
    return html.escape(s, quote=True)


# Defensive name-rewrite. The gitignored assessment files are
# scrubbed at the source level (see src/scrub_student_narration.py),
# but if a future file slips through with the student's name in
# narrator voice, this pass replaces it before the HTML is written.
# Verbatim quoted text (in `"`, `'`, or `` ` ``) is left alone, so
# OCR transcripts and chemical equations are not touched.
def narration_rewrite(text: str) -> str:
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in ('"', "'", "`"):
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            if j >= n:
                out.append(text[i:])
                break
            out.append(text[i:j + 1])
            i = j + 1
            continue
        j = i
        while j < n and text[j] not in ('"', "'", "`"):
            j += 1
        seg = text[i:j]
        # Possessive: Will's -> the student's (case-insensitive)
        seg = re.sub(r"(?<![A-Za-z])will's(?![A-Za-z])", "the student's", seg, flags=re.IGNORECASE)
        # The student -> You (the narrator voice addressing the
        # reader). The source files were written in third person
        # ("The student wrote X"); for the published page this
        # reads more naturally as second person ("You wrote X").
        seg = re.sub(r"\bThe student\b", "You", seg)
        seg = re.sub(r"\bThe student's\b", "Your", seg)
        # Plain Will/Will's in narration -> You / the student's
        # (case-sensitive: only the proper noun, not modal verb)
        seg = re.sub(r"\bWill's\b", "the student's", seg)
        seg = re.sub(r"\bWill\b", "You", seg)
        # Pronouns referring to the student in narrator voice —
        # rewrite to second-person for the feedback tone, and to
        # avoid leaking the (now anonymised) identity via gender.
        # The pattern matches " He ", "He ", " He.", etc.
        # We do NOT touch "He " in chemical text (e.g. "He" in
        # "Helium" — that gets past, since the markscheme will
        # only say "Helium" in full, and the OCR transcripts are
        # not rendered to the public page).
        seg = re.sub(r"\bhe scored\b", "you scored", seg)
        seg = re.sub(r"\bhe was\b", "you were", seg)
        seg = re.sub(r"\bhe is\b", "you are", seg)
        seg = re.sub(r"\bhe has\b", "you have", seg)
        seg = re.sub(r"\bhe did\b", "you did", seg)
        seg = re.sub(r"\bhe got\b", "you got", seg)
        seg = re.sub(r"\bhe left\b", "you left", seg)
        seg = re.sub(r"\bhe wrote\b", "you wrote", seg)
        seg = re.sub(r"\bhe drew\b", "you drew", seg)
        seg = re.sub(r"\bhe used\b", "you used", seg)
        seg = re.sub(r"\bhe gained\b", "you gained", seg)
        seg = re.sub(r"\bhe handled\b", "you handled", seg)
        seg = re.sub(r"\bhe answered\b", "you answered", seg)
        seg = re.sub(r"\bhe identified\b", "you identified", seg)
        seg = re.sub(r"\bhe lost\b", "you lost", seg)
        # Possessive "his" only when referring to the student's
        # answer/working/reasoning; this is safer than a blanket
        # rewrite.
        seg = re.sub(r"\bhis best-fit lines\b", "your best-fit lines", seg)
        seg = re.sub(r"\bhis answer\b", "your answer", seg)
        seg = re.sub(r"\bhis reasoning\b", "your reasoning", seg)
        seg = re.sub(r"\bhis script\b", "your script", seg)
        seg = re.sub(r"\bhis handwriting\b", "your handwriting", seg)
        seg = re.sub(r"\bhis photo\b", "the photo", seg)
        # Strip absolute dev paths (e.g. D:\dev\codex-sandboxes\...)
        # that have leaked into the assessment files. Replace with
        # a generic "(prompt template)" reference. The path itself
        # leaks the developer's filesystem layout, which is a
        # privacy concern on a public site.
        seg = re.sub(r"`[A-Za-z]:\\\\[^\s`]+`", "`(prompt template)`", seg)
        out.append(seg)
        i = j
    return "".join(out)


def md_to_html_simple(s: str) -> str:
    """Lightweight markdown -> HTML for short strings (observations,
    verdicts). Handles: **bold**, *italic*, `code`, paragraphs,
    bullet lists. Deliberately not a full markdown parser — we
    control the input and want predictable output."""
    if not s: return ""
    s = esc(s)
    # Bold
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    # Italic
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    # Inline code
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    # Split into paragraphs / lists
    lines = s.splitlines()
    out = []
    in_ul = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("")
            continue
        if stripped.startswith("- "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"  <li>{stripped[2:]}</li>")
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<p>{stripped}</p>")
    if in_ul:
        out.append("</ul>")
    # Wrap bare lines in <p> if not already
    final = []
    for line in out:
        if line == "":
            continue
        if not line.startswith("<"):
            final.append(f"<p>{line}</p>")
        else:
            final.append(line)
    return "\n".join(final)


def render_hero_html(meta: dict, summary: dict, kvdb_bucket: str) -> str:
    total_avail = summary.get("total_available") or meta.get("total_marks_available", 0)
    total_awarded = summary.get("total_awarded") or 0
    pct = round(100 * total_awarded / total_avail) if total_avail else 0
    # UMS grade boundaries for GCSE (AQA). Good enough for a friendly
    # descriptor; the official conversion is in the spec.
    if pct >= 70:   grade = "Grade 7+"
    elif pct >= 60: grade = "Grade 6"
    elif pct >= 50: grade = "Grade 5"
    elif pct >= 40: grade = "Grade 4"
    else:           grade = "Below grade 4"
    return f"""
<section class="hero">
  <div class="hero-left">
    <span class="hero-eyebrow">{esc(meta.get('board', ''))} · {esc(meta.get('spec', ''))} · {esc(meta.get('tier', ''))}</span>
    <h1>Your {esc(meta.get('title', ''))} result</h1>
    <div class="hero-meta">
      <span>Paper <strong>{esc(meta.get('spec', ''))}/{esc(meta.get('paper', ''))}</strong></span>
      <span>Sitting <strong>{esc(meta.get('sitting_date', ''))}</strong></span>
      <span>Total available <strong>{total_avail}</strong></span>
    </div>
  </div>
  <div class="score-block">
    <div class="score-main">
      <span class="awarded">{total_awarded}</span>
      <span class="separator">/</span>
      <span class="available">{total_avail}</span>
    </div>
    <div class="score-sub">
      <span class="pct">{pct}%</span>
      <span class="grade">{esc(grade)}</span>
    </div>
  </div>
</section>
"""


def render_question_html(q: dict, slug: str) -> str:
    """Render one question as an accordion section. The first
    question is open by default on all viewports; subsequent
    questions are collapsed. The collapsed-state headline shows
    the question's printed_context (one-line summary) rather than
    the sub-part IDs, so it reads as a real headline and not a
    chevron-in-void."""
    qnum = q["qnum"]
    q_label = q["q_label"]
    total_a = q.get("total_awarded", 0)
    total_p = q.get("total_available", 0)
    is_first = str(qnum).lstrip("0") == "1"
    open_state = "true" if is_first else "false"
    q_sub = q.get("printed_context") or q.get("subparts_covered", "")
    context_html = ""
    if q.get("printed_context"):
        context_html = f'<div class="qcontext">{esc(q["printed_context"])}</div>'

    # Per-criterion blocks
    crit_html_parts = []
    for c in q.get("criteria", []):
        crit_html_parts.append(render_criterion_html(c, slug=slug, qnum=qnum, cnum=len(crit_html_parts) + 1))
    crit_html = "\n".join(crit_html_parts) if crit_html_parts else '<p class="qsub">No per-criterion details available.</p>'

    # Legibility assessment. Renders a small block in the per-Q
    # body (after the criteria, before the question summary).
    # The full breakdown (score, ocr_mode, reason) goes in the
    # Assessor Notes collapsed section via the parent renderer.
    legibility = q.get("legibility") or {}
    legibility_html = ""
    if legibility.get("legibility_score") is not None:
        # Visible to Will: a one-line "letter to you" from the
        # marker about this question's handwriting. The score
        # and mode go in the assessor-only block.
        feedback = esc(legibility.get("student_feedback") or "")
        score = legibility["legibility_score"]
        ocr_mode = esc(legibility.get("ocr_mode") or "")
        mode_label = ocr_mode.replace("_", " ") if ocr_mode else "n/a"
        flagged_class = " flagged" if legibility.get("flag_for_recheck") else ""
        legibility_html = (
            f'<div class="legibility{flagged_class}">'
            f'<span class="legibility-tag">legibility {score}/5</span>'
            f'<span class="legibility-mode">{mode_label}</span>'
            f'<p class="legibility-feedback">{feedback}</p>'
            f'</div>'
        )

    # Legibility indicator in the Q head. Always visible (even
    # when the Q is collapsed), so Aaron can see the handwriting
    # state at a glance across all 9 questions without expanding
    # each one. Format: "L 4/5" for normal scores, with a flag
    # class for low scores (orange tint), and the full
    # "needs recheck" badge in addition when the score is
    # particularly low (<= 2 or unreadable).
    legibility_chip = ""
    legibility_score = legibility.get("legibility_score")
    if legibility_score is not None:
        # Color tier: 4-5 (good) = default muted, 3 (caution) =
        # yellow tint, 0-2 (bad) = red/orange tint
        if legibility_score <= 2:
            tier_class = " leg-chip-bad"
        elif legibility_score == 3:
            tier_class = " leg-chip-caution"
        else:
            tier_class = " leg-chip-good"
        legibility_chip = (
            f'<span class="leg-chip{tier_class}" '
            f'title="Handwriting legibility for this question (0-5)">'
            f'L {legibility_score}/5'
            f'</span>'
        )
    recheck_badge = ""
    if legibility.get("flag_for_recheck"):
        recheck_badge = (
            '<span class="recheck-badge" '
            'title="Handwriting was hard to read; recheck the original photo against the marking.">'
            'needs recheck</span>'
        )

    # Question summary
    q_summary_html = ""
    if q.get("q_summary_md"):
        q_summary_html = f'<div class="qsummary">{md_to_html_simple(q["q_summary_md"])}</div>'

    return f"""
<section class="qsection" data-open="{open_state}" id="q-{qnum}">
  <button class="qhead" type="button" aria-expanded="{open_state}" aria-controls="q-{qnum}-body">
    <span class="qnum">Q{qnum}</span>
    <span class="qmain">
      <span class="qsub">{esc(q_sub)}</span>
    </span>
    <span class="qscore">
      {legibility_chip}
      {recheck_badge}
      <span class="a">{total_a}</span>
      <span class="s">/</span>
      <span class="b">{total_p}</span>
    </span>
    <svg class="chevron" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  </button>
  <div class="qbody" id="q-{qnum}-body">
    {context_html}
    <div class="criteria">
      {crit_html}
    </div>
    {legibility_html}
    {q_summary_html}
  </div>
</section>
"""


def render_criterion_html(c: dict, slug: str = "", qnum: str = "", cnum: int = 0) -> str:
    """Render one criterion block with a per-criterion feedback
    widget. The criterion_id is stable for a given (paper_slug,
    question_number, criterion_index_within_question) so JS
    localStorage keys don't collide across papers."""
    ao = esc(c.get("ao", ""))
    subq = esc(c.get("subq", ""))
    decision = c.get("decision", "AWARD")
    decision_label = decision.replace("_", " ")
    verdict_class = decision   # AWARD, NOT_AWARD, NOT_APPLICABLE
    marks_a = c.get("marks_awarded", 0)
    marks_p = c.get("marks_available", 0)
    criterion_id = f"{slug}_q{qnum}_c{cnum}" if slug and qnum else f"c{cnum}"

    indicative_html = ""
    if c.get("indicative"):
        items = "".join(f"<li>{esc(i)}</li>" for i in c["indicative"])
        indicative_html = f'<div><span class="indicative-label">Indicative content</span><ul class="indicative">{items}</ul></div>'

    justification = esc(c.get("justification", ""))

    return f"""
<div class="criterion {verdict_class}" data-criterion-id="{esc(criterion_id)}" data-verdict="{verdict_class}">
  <div class="criterion-head">
    <span class="marks">{marks_a}/{marks_p}</span>
    {f'<span class="ao">{ao}</span>' if ao and ao != '—' else ''}
    <span class="subq">{subq}</span>
    <span class="verdict-pill {verdict_class}">{esc(decision_label)}</span>
  </div>
  <div class="criterion-body">
    {indicative_html}
    <p class="justification">{justification}</p>
  </div>
  <div class="feedback" data-mode="" data-criterion-id="{esc(criterion_id)}">
    <span class="feedback-prompt"><span class="icon">↪</span> Do you agree with this mark?</span>
    <div class="fb-actions" role="group" aria-label="Verdict">
      <button type="button" class="fb-btn AWARD" data-mode="AWARD"><span class="icon">✓</span> Agree</button>
      <button type="button" class="fb-btn DISAGREE" data-mode="DISAGREE"><span class="icon">↺</span> Disagree</button>
      <button type="button" class="fb-btn NOTE" data-mode="NOTE"><span class="icon">✎</span> I read it as…</button>
    </div>
    <textarea class="fb-note" data-mode="disagree" placeholder="What did you write, and why does the mark seem wrong?" rows="2"></textarea>
    <textarea class="fb-note" data-mode="note" placeholder="Tell us what you read on the page (e.g. 'I wrote 0.5 mol, not 0.05')." rows="2"></textarea>
    <div class="fb-actions">
      <button type="button" class="fb-save" disabled>Save feedback</button>
      <span class="fb-status" aria-live="polite"></span>
    </div>
  </div>
</div>
"""


def render_feedback_html(kvdb_bucket: str) -> str:
    """The 'give feedback' section. Links to a private URL that
    PUTs to the per-paper KVdb bucket."""
    if not kvdb_bucket:
        return ""
    url = f"https://kvdb.io/{kvdb_bucket}/student-feedback"
    return f"""
<section class="feedback" id="feedback">
  <h2>Give feedback per mark</h2>
  <p>For each criterion, you can <strong>agree</strong>, <strong>disagree</strong>, or add a note about what you read on the page. Each click is anonymous.</p>
  <a class="btn" href="{esc(url)}" target="_blank" rel="noopener">Open the feedback page →</a>
</section>
"""


def render_rail_html(meta: dict, summary: dict, questions: list[dict]) -> str:
    """The right-rail summary card. On desktop, sticky to the
    viewport. On mobile, the rail would duplicate the hero, so
    it's intentionally hidden via CSS at narrow viewports."""
    total_avail = summary.get("total_available") or meta.get("total_marks_available", 0)
    total_awarded = summary.get("total_awarded") or 0
    pct = round(100 * total_awarded / total_avail) if total_avail else 0
    n_criteria = sum(len(q.get("criteria", [])) for q in questions)
    return f"""
<aside class="rail">
  <div class="rail-inner">
    <div class="rail-card">
      <h3>At a glance</h3>
      <div class="total">{total_awarded}<span class="total-sub"> / {total_avail} · {pct}%</span></div>
      <div class="progress" aria-label="Progress">
        <div class="bar" style="width: {pct}%;"></div>
      </div>
      <p class="feedback-status" id="rail-feedback-status">
        <span class="count" id="rail-feedback-count">0</span> / {n_criteria} criteria responded
      </p>
    </div>
    <div class="rail-card">
      <h3>Send everything</h3>
      <p style="margin: 0; font-size: var(--t-sm); color: var(--text-muted);">Reviewed every question? Send the whole batch as one PUT.</p>
      <button type="button" class="btn-primary" id="send-all-btn" disabled>Send all feedback</button>
      <button type="button" class="btn-link" id="clear-all-btn">Clear all responses</button>
    </div>
    <div class="rail-card">
      <h3>Notes for next time</h3>
      <p style="margin: 0; font-size: var(--t-sm); color: var(--text-muted);">Pattern-level observations get logged to the calibration file when you save. The next assessment is marked against what you've shown you can do.</p>
    </div>
  </div>
</aside>
"""


def render_per_batch_html(meta: dict, summary: dict, questions: list[dict], student: dict, kvdb_bucket: str) -> str:
    hero = render_hero_html(meta, summary, kvdb_bucket)
    q_blocks = "\n".join(render_question_html(q, meta["slug"]) for q in questions)
    rail = render_rail_html(meta, summary, questions)

    obs_html = ""
    if summary.get("observations_md"):
        obs_html = f'<section class="callout obs"><h2>Cross-paper observations</h2>{md_to_html_simple(summary["observations_md"])}</section>'

    # Legibility rollup (assessor-only, in the collapsed notes).
    # Shows per-Q legibility score, ocr_mode, and a one-line
    # assessor-side reason. The student_feedback text is also
    # shown here for the assessor's audit trail; the student
    # already saw it in the per-Q body.
    legibility_rows = []
    for q in questions:
        leg = q.get("legibility") or {}
        score = leg.get("legibility_score")
        if score is None:
            continue
        ocr_mode = esc(leg.get("ocr_mode") or "n/a")
        reason = esc(leg.get("reason") or "")
        feedback = esc(leg.get("student_feedback") or "")
        flag = " ⚠ needs recheck" if leg.get("flag_for_recheck") else ""
        legibility_rows.append(
            f'<tr>'
            f'<td>Q{esc(q["qnum"])}</td>'
            f'<td>{score}/5{flag}</td>'
            f'<td>{ocr_mode}</td>'
            f'<td>{reason}</td>'
            f'<td>{feedback}</td>'
            f'</tr>'
        )
    legibility_table = ""
    if legibility_rows:
        legibility_table = (
            f'<h3>Legibility per question</h3>'
            f'<table class="legibility-table">'
            f'<thead><tr><th>Q</th><th>Score</th><th>OCR mode</th><th>Reason (assessor)</th><th>Feedback to student</th></tr></thead>'
            f'<tbody>{"".join(legibility_rows)}</tbody>'
            f'</table>'
        )

    # Assessor-only notes (pipeline meta: OCR blockers, marking uncertainty,
    # pipeline verdict). Rendered as a collapsed <details> so the student's
    # primary feedback dominates the page and the meta is one click away
    # for the assessor. The student never sees this on a normal read.
    assessor_notes_html = ""
    if summary.get("assessor_notes_md") or legibility_table:
        # md_to_html_simple already wraps paragraphs in <p>; the <details>
        # block uses <summary> as the clickable label.
        assessor_notes_html = (
            f'<details class="assessor-notes">'
            f'<summary>Assessor notes (pipeline meta, hidden by default)</summary>'
            f'<div class="assessor-notes-body">'
            f'{legibility_table}'
            f'{md_to_html_simple(summary["assessor_notes_md"]) if summary.get("assessor_notes_md") else ""}'
            f'</div>'
            f'</details>'
        )

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    display_name = student.get("display_name", DISPLAY_NAME_OVERRIDE)
    kvdb_bucket_esc = esc(kvdb_bucket or "")

    # The JS below uses double-brace escaping ({{...}}) for the
    # f-string literal braces; the single-brace {display_name}
    # etc. are the f-string substitutions.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light dark">
  <meta name="description" content="Per-assessment result for {esc(display_name)} — {esc(meta.get('title', ''))}">
  <meta name="robots" content="noindex">
  <title>{esc(display_name)} — {esc(meta.get('title', ''))}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap">
  <link rel="stylesheet" href="../assets/css/styles.css">
</head>
<body data-kvdb-bucket="{kvdb_bucket_esc}">
  <header class="topbar">
    <div class="topbar-inner">
      <a href="../index.html" class="brand" aria-label="Back to dashboard">
        <span class="brand-mark">e</span>
        <span>examiner</span>
      </a>
      <nav class="topnav">
        <a href="../index.html">All assessments</a>
      </nav>
    </div>
  </header>

  {hero}

  <div class="layout">
    <main class="content">
      <div class="section-header">
        <h2>Per-question breakdown</h2>
        <span class="meta">{len(questions)} questions</span>
      </div>
      <div class="questions">
        {q_blocks}
      </div>
      {obs_html}
      {assessor_notes_html}
    </main>
    {rail}
  </div>

  <footer class="footer">
    <div class="row">Marked by Jimothy's second-pair-of-eyes pipeline · Errors flagged for human review · Last updated {updated}</div>
  </footer>

  <script src="../assets/js/feedback.js" defer></script>
</body>
</html>
"""


def render_index_html(batches: list[dict]) -> str:
    """Dashboard listing all assessments. No student name on the
    index — that's reserved for the per-assessment page only."""
    if not batches:
        cards = '<div class="empty-state"><h2>No assessments yet</h2><p>Run <code>src/mark_batch.py</code> on a paper, then re-run publish.</p></div>'
    else:
        items = []
        for b in batches:
            total_a = b.get("total_awarded", 0) or 0
            total_p = b.get("total_available", 0) or 0
            pct = round(100 * total_a / total_p) if total_p else 0
            slug = b["slug"]
            title = b.get("title") or slug
            sub = f"{b.get('board','')} · {b.get('spec','')} · {b.get('tier','')} · {b.get('sitting_date','')}"
            items.append(f"""
<a class="batch-card" href="assessments/{esc(slug)}.html">
  <span class="badge">Result</span>
  <h3>{esc(title)}</h3>
  <p class="meta">{esc(sub)}</p>
  <div class="score">
    <span class="awarded">{total_a}</span><span class="denom">/ {total_p}</span>
    <span class="pct">{pct}%</span>
  </div>
</a>
""")
        cards = f'<ul class="batch-list">{"".join(items)}</ul>'

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light dark">
  <meta name="description" content="GCSE self-mark dashboard. Per-assessment results, feedback, and per-criterion markscheme detail.">
  <title>examiner — all assessments</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap">
  <link rel="stylesheet" href="assets/css/styles.css">
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <a href="index.html" class="brand" aria-label="Dashboard">
        <span class="brand-mark">e</span>
        <span>examiner</span>
      </a>
      <nav class="topnav">
        <a href="index.html" class="active">Dashboard</a>
      </nav>
    </div>
  </header>
  <div class="dash-wrap">
    <header class="dash-hero">
      <span class="hero-eyebrow">Dashboard</span>
      <h1>All assessed papers</h1>
      <p>Open a paper to see the per-criterion breakdown and send feedback per mark.</p>
    </header>
    {cards}
  </div>
  <footer class="footer">
    <div class="row">Marked by Jimothy's second-pair-of-eyes pipeline · Last updated {updated}</div>
  </footer>
</body>
</html>
"""


# ---------- Orchestration ----------

def publish_one(slug: str, student: dict, dry_run: bool = False) -> dict:
    """Render one batch to pages/assessments/<slug>.html. Returns
    a metadata dict for the index."""
    meta = read_paper_metadata(slug)
    # Self-healing bucket: if papers/<slug>/kvdb-bucket.txt is missing
    # or the id is 404 on kvdb.io, create a new one. The recovery email
    # is the active identity's staging recipient (Aaron's, in practice).
    recovery_email = student.get("recipient_email_staging", "")
    if dry_run:
        kvdb_bucket = read_kvdb_bucket_from_paper(slug)
        bucket_rotated = False
        print(f"  [dry-run] bucket: {kvdb_bucket or '(none)'}")
    else:
        kvdb_bucket, bucket_rotated = ensure_kvdb_bucket(slug, recovery_email)
    summary = parse_summary(slug)

    questions = []
    for q_row in summary.get("q_rows", []):
        # SUMMARY.md uses "Q1" (single digit); the files are
        # "Q01.marking.md" (zero-padded). Normalise.
        q_short = q_row["q"]                              # "Q1"
        q_padded = "Q" + q_short.lstrip("Q").zfill(2)    # "Q01"
        q = parse_question_marking(slug, q_padded)
        if q is None:
            print(f"  warning: no marking file for {q_short} (looked for {slug}/{q_padded}.marking.md)", file=sys.stderr)
            continue
        # Use the per-Q .marking.md as source of truth for the
        # per-criterion breakdown; the SUMMARY.md row carries
        # the high-level notes.
        q["high_level_notes"] = q_row.get("notes", "")
        questions.append(q)

    # Sort numerically by Q number
    questions.sort(key=lambda q: int(q["qnum"]))

    html_doc = narration_rewrite(render_per_batch_html(meta, summary, questions, student, kvdb_bucket))

    out = PAGES_ASSESSMENTS / f"{slug}.html"
    if not dry_run:
        PAGES_ASSESSMENTS.mkdir(parents=True, exist_ok=True)
        out.write_text(html_doc, encoding="utf-8")
    print(f"  rendered {out}  ({len(html_doc)} bytes,  {len(questions)} questions)")

    return {
        **meta,
        "total_available": summary.get("total_available") or sum(q["total_available"] for q in questions),
        "total_awarded": summary.get("total_awarded") or sum(q["total_awarded"] for q in questions),
        "kvdb_bucket": kvdb_bucket,
        "bucket_rotated": bucket_rotated,
    }


def publish_index(batches: list[dict], dry_run: bool = False) -> None:
    html_doc = narration_rewrite(render_index_html(batches))
    out = PAGES / "index.html"
    if not dry_run:
        PAGES.mkdir(parents=True, exist_ok=True)
        out.write_text(html_doc, encoding="utf-8")
    print(f"  rendered {out}  ({len(html_doc)} bytes,  {len(batches)} assessments)")


def copy_assets(dry_run: bool = False) -> None:
    """Copy the stylesheet + client-side JS to pages/assets/.
    The Pages workflow uploads pages/ as the artifact, so these
    are what the live site actually serves."""
    if not SRC_CSS.is_file():
        raise FileNotFoundError(f"{SRC_CSS} does not exist. The CSS source is missing.")
    src_js = REPO_ROOT / "src" / "assets" / "js" / "feedback.js"
    if not src_js.is_file():
        raise FileNotFoundError(f"{src_js} does not exist. The client-side JS is missing.")
    if not dry_run:
        PAGES_ASSETS.mkdir(parents=True, exist_ok=True)
        PAGES_ASSETS.joinpath("css").mkdir(parents=True, exist_ok=True)
        PAGES_ASSETS.joinpath("js").mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC_CSS, PAGES_CSS)
        shutil.copy2(src_js, PAGES_ASSETS / "js" / "feedback.js")
    print(f"  copied {SRC_CSS} -> {PAGES_CSS}")
    print(f"  copied {src_js} -> {PAGES_ASSETS / 'js' / 'feedback.js'}")


def discover_batches() -> list[str]:
    """Return all slugs that have a SUMMARY.md (i.e. a complete marking)."""
    out = []
    if not ASSESSMENTS.is_dir():
        return out
    for p in sorted(ASSESSMENTS.iterdir()):
        if not p.is_dir():
            continue
        if (p / "SUMMARY.md").is_file():
            out.append(p.name)
    return out


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render the per-assessment HTML pages and the dashboard index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--batch", help="Render one batch (paper slug).")
    g.add_argument("--all", action="store_true", help="Render every batch with a SUMMARY.md.")
    p.add_argument("--yes", action="store_true", help="Skip the per-file confirmation prompt.")
    p.add_argument("--dry-run", action="store_true", help="Don't write any files. Print what would happen.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    student = read_student_json(require_recipient=False)
    print(f"Active identity:    {student['_active_identity']!r}  (from private/active.json)")
    print(f"Student display name: {student['display_name']!r}")
    if student.get('recipient_email_staging'):
        print(f"Staging recipient:    {student['recipient_email_staging']!r}")

    if args.batch:
        slugs = [args.batch]
    else:
        slugs = discover_batches()
        if not slugs:
            print("No batches found (no assessments/*/SUMMARY.md). Nothing to do.", file=sys.stderr)
            return 1
        print(f"Discovered {len(slugs)} batch(es): {slugs}")

    if not args.yes and not args.dry_run:
        print("This will overwrite:")
        for s in slugs:
            print(f"  pages/assessments/{s}.html")
        print(f"  pages/index.html")
        print(f"  pages/{PAGES_CSS.relative_to(PAGES)}")
        resp = input("Continue? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    print(f"Rendering {len(slugs)} batch(es)...")
    metas = []
    for s in slugs:
        try:
            metas.append(publish_one(s, student, dry_run=args.dry_run))
        except Exception as e:
            print(f"  ERROR rendering {s}: {e}", file=sys.stderr)
            return 1

    print("Rendering dashboard index...")
    publish_index(metas, dry_run=args.dry_run)

    print("Copying stylesheet + client JS...")
    copy_assets(dry_run=args.dry_run)

    print()
    if args.dry_run:
        print("Dry run. No files written.")
        print()
        print("Next step: review the rendered HTML at pages/assessments/<slug>.html")
        print("in a browser, then re-run without --dry-run.")
    else:
        print("Done.")
        print()
        print("Next step: review the diff, then:")
        print("  git add pages/")
        print('  git commit -m "publish: render assessment HTML for <slug>"')
        print("  git push origin main")
        print("The Pages deploy workflow will pick it up on the push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
