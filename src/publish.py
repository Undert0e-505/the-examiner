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
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path("D:/dev/the-examiner")
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


def read_student_json(require_recipient: bool = False) -> dict:
    """Read the gitignored source-of-truth file.

    If require_recipient is True, refuse to run if the staging
    recipient email is still the placeholder. publish.py does not
    need the recipient email; email.py does.
    """
    path = PRIVATE / "student.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} does not exist. Create it from the schema in "
            f"docs/PRIVACY.md before running publish.py."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if require_recipient:
        staging = data.get("recipient_email_staging", "")
        if "REPLACE_WITH_AARONS_EMAIL" in staging:
            raise RuntimeError(
                f"{path} still has the placeholder recipient_email_staging. "
                f"Edit the file and replace 'REPLACE_WITH_AARONS_EMAIL@example.com' "
                f"with Aaron's real email before running email.py."
            )
    for k in ("display_name", "first_name_for_salutation"):
        if k not in data:
            raise KeyError(f"{path} is missing the required field '{k}'.")
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

    # Observations — pull the paragraphs from "## 3. Cross-paper observations"
    obs = extract_section(content, "## 3. Cross-paper observations", "## 4.")

    # Verdict
    verdict = extract_section(content, "## 4. Pipeline verdict", None)

    return {
        "paper_code": paper_code,
        "sitting": sitting,
        "total_available": total_available,
        "total_awarded": total_awarded,
        "q_rows": q_rows,
        "observations_md": obs,
        "verdict_md": verdict,
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

    # Question summary
    q_summary = ""
    m = re.search(r"## 3\. Question summary\s*\n+([^#]+)", content, re.DOTALL)
    if m: q_summary = m.group(1).strip()

    return {
        "qnum": qnum,
        "q_label": q_label,
        "total_available": total_avail or sum(c.get("marks_available", 0) for c in criteria),
        "total_awarded": sum(c.get("marks_awarded", 0) for c in criteria),
        "subparts_covered": subparts_covered,
        "printed_context": printed_context,
        "criteria": criteria,
        "q_summary_md": q_summary,
    }


def parse_criterion_block(block: str) -> dict | None:
    """Parse a single criterion block from a Q*.marking.md file."""
    # The header is "AO3 -- 2 mark(s)"
    header_m = re.match(r"([^\n]+?)\s*\n", block)
    if not header_m:
        return None
    header = header_m.group(1).strip()
    # Extract AO and marks
    ao_m = re.match(r"^(AO\d+|—|null|-)\s*--\s*(\d+)\s*marks?", header)
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
# scrubbed at the source level (see src/scrub_will_narration.py),
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
    return f"""
<section class="hero">
  <span class="eyebrow">{esc(meta.get('board', ''))} · {esc(meta.get('spec', ''))} · {esc(meta.get('tier', ''))}</span>
  <h1>Your {esc(meta.get('title', ''))} result</h1>
  <div class="big">
    <span class="awarded">{total_awarded}</span>
    <span class="separator">/</span>
    <span class="available">{total_avail}</span>
  </div>
  <div class="pct">{pct}%</div>
  <p class="meta">Paper code {esc(meta.get('spec', ''))}/{esc(meta.get('paper', ''))} · Sitting {esc(meta.get('sitting_date', ''))}</p>
</section>
"""


def render_question_html(q: dict, slug: str) -> str:
    """Render one question as an accordion section."""
    qnum = q["qnum"]
    q_label = q["q_label"]
    total_a = q.get("total_awarded", 0)
    total_p = q.get("total_available", 0)
    context_html = ""
    if q.get("printed_context"):
        context_html = f'<div class="context">{esc(q["printed_context"])}</div>'

    # Per-criterion blocks
    crit_html_parts = []
    for c in q.get("criteria", []):
        crit_html_parts.append(render_criterion_html(c))
    crit_html = "\n".join(crit_html_parts) if crit_html_parts else '<p class="subq">No per-criterion details available.</p>'

    # Question summary
    q_summary_html = ""
    if q.get("q_summary_md"):
        q_summary_html = f'<div class="qsummary">{md_to_html_simple(q["q_summary_md"])}</div>'

    return f"""
<section class="qsection" data-open="false">
  <div class="qhead" tabindex="0" role="button" aria-expanded="false" aria-controls="q-{qnum}-body">
    <div class="qlabel">
      <h2 class="qtitle">Question {qnum}</h2>
      <p class="qsubtitle">{esc(q.get('subparts_covered', ''))}</p>
    </div>
    <div class="qscore">
      <span class="num"><span class="awarded">{total_a}</span><span class="denom">/ {total_p}</span></span>
    </div>
    <svg class="chevron" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M5 7.5l5 5 5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  </div>
  <div class="qbody" id="q-{qnum}-body">
    {context_html}
    {crit_html}
    {q_summary_html}
  </div>
</section>
"""


def render_criterion_html(c: dict) -> str:
    ao = esc(c.get("ao", ""))
    subq = esc(c.get("subq", ""))
    decision = c.get("decision", "AWARD")
    decision_label = decision.replace("_", " ")
    verdict_class = decision   # AWARD, NOT_AWARD, NOT_APPLICABLE
    marks_a = c.get("marks_awarded", 0)
    marks_p = c.get("marks_available", 0)

    indicative_html = ""
    if c.get("indicative"):
        items = "".join(f"<li>{esc(i)}</li>" for i in c["indicative"])
        indicative_html = f'<div class="indicative"><span class="label">Indicative content</span><ul>{items}</ul></div>'

    justification = esc(c.get("justification", ""))

    return f"""
<div class="criterion {verdict_class}">
  <div class="criterion-head">
    <span class="num">×{marks_p}</span>
    {f'<span class="ao">{ao}</span>' if ao and ao != '—' else ''}
    <span class="subq">{subq}</span>
    <span class="verdict {verdict_class}"><span class="dot"></span>{esc(decision_label)}</span>
  </div>
  {indicative_html}
  <div class="justification">{justification}</div>
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


def render_per_batch_html(meta: dict, summary: dict, questions: list[dict], student: dict, kvdb_bucket: str) -> str:
    hero = render_hero_html(meta, summary, kvdb_bucket)
    q_blocks = "\n".join(render_question_html(q, meta["slug"]) for q in questions)

    obs_html = ""
    if summary.get("observations_md"):
        obs_html = f'<section class="callout note"><h2>Cross-paper observations</h2>{md_to_html_simple(summary["observations_md"])}</section>'

    verdict_html = ""
    if summary.get("verdict_md"):
        verdict_html = f'<section class="callout"><h2>Pipeline verdict</h2>{md_to_html_simple(summary["verdict_md"])}</section>'

    feedback = render_feedback_html(kvdb_bucket)

    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    display_name = student.get("display_name", DISPLAY_NAME_OVERRIDE)

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
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,500;12..96,600;12..96,700;12..96,800&family=Inter:wght@400;500;600;700&display=swap">
  <link rel="stylesheet" href="assets/css/styles.css">
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <a href="index.html" aria-label="Back to dashboard">
        <span class="logo">E</span>
        <span>examiner</span>
      </a>
      <nav>
        <a href="index.html">All assessments</a>
      </nav>
    </div>
  </header>
  <main>
    {hero}
    {q_blocks}
    {obs_html}
    {verdict_html}
    {feedback}
  </main>
  <footer class="footer">
    Last updated {updated}. Automated result — for human review, contact the assessor.
  </footer>
  <script>
    // Accordion: open on click or keyboard activation.
    document.querySelectorAll('.qhead').forEach(function(head) {{
      head.addEventListener('click', function() {{
        var section = head.closest('.qsection');
        var open = section.getAttribute('data-open') === 'true';
        section.setAttribute('data-open', open ? 'false' : 'true');
        head.setAttribute('aria-expanded', open ? 'false' : 'true');
      }});
      head.addEventListener('keydown', function(e) {{
        if (e.key === 'Enter' || e.key === ' ') {{
          e.preventDefault();
          head.click();
        }}
      }});
    }});
  </script>
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
  <title>GCSE self-mark dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,500;12..96,600;12..96,700;12..96,800&family=Inter:wght@400;500;600;700&display=swap">
  <link rel="stylesheet" href="assets/css/styles.css">
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner wide">
      <a href="index.html" aria-label="Dashboard">
        <span class="logo">E</span>
        <span>examiner</span>
      </a>
      <nav>
        <a href="index.html" class="active">Dashboard</a>
      </nav>
    </div>
  </header>
  <main class="wide">
    <section class="hero" style="text-align:left;">
      <span class="eyebrow">Dashboard</span>
      <h1>GCSE self-mark</h1>
      <p style="color:var(--text-muted); margin-top:var(--space-3);">All assessed papers. Tap a card to see the per-criterion breakdown and give feedback per mark.</p>
    </section>
    {cards}
  </main>
  <footer class="footer">
    Last updated {updated}. Automated result — for human review, contact the assessor.
  </footer>
</body>
</html>
"""


# ---------- Orchestration ----------

def publish_one(slug: str, student: dict, dry_run: bool = False) -> dict:
    """Render one batch to pages/assessments/<slug>.html. Returns
    a metadata dict for the index."""
    meta = read_paper_metadata(slug)
    kvdb_bucket = read_kvdb_bucket_from_paper(slug)
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
    }


def publish_index(batches: list[dict], dry_run: bool = False) -> None:
    html_doc = narration_rewrite(render_index_html(batches))
    out = PAGES / "index.html"
    if not dry_run:
        PAGES.mkdir(parents=True, exist_ok=True)
        out.write_text(html_doc, encoding="utf-8")
    print(f"  rendered {out}  ({len(html_doc)} bytes,  {len(batches)} assessments)")


def copy_css(dry_run: bool = False) -> None:
    if not SRC_CSS.is_file():
        raise FileNotFoundError(f"{SRC_CSS} does not exist. The CSS source is missing.")
    if not dry_run:
        PAGES_ASSETS.mkdir(parents=True, exist_ok=True)
        PAGES_ASSETS.joinpath("css").mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC_CSS, PAGES_CSS)
    print(f"  copied {SRC_CSS} -> {PAGES_CSS}")


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
    print(f"Student display name: {student['display_name']!r}")
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

    print("Copying stylesheet...")
    copy_css(dry_run=args.dry_run)

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
