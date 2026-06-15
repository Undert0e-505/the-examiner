"""
scripts/diff_m3_vs_canonical.py — per-criterion diff of m3 (fork)
marking files vs canonical (Codex) marking files for the
chemistry paper. Prints a table of the differences and
summarises the per-question tallies.

Run from D:\\dev\\the-examiner-m3 with the same Python (3.10.11).
"""
import sys
from pathlib import Path

sys.path.insert(0, r"D:\dev\the-examiner-m3\src")
sys.path.insert(0, r"D:\dev\the-examiner\src")

# The fork's publish.py is the same code, but ASSESSMENTS is
# hard-coded to the fork's path. To compare both, we'll read the
# raw files and parse them with a slightly different trick:
# monkey-patch ASSESSMENTS to the canonical dir for the canonical
# pass, then back to the fork for the m3 pass.
import importlib

import publish as fork_publish  # noqa: E402

SLUG = "aqa-84621h-chemistry-higher-2024-05"
CANON = Path(r"D:\dev\the-examiner\assessments") / SLUG
FORK = Path(r"D:\dev\the-examiner-m3\assessments") / SLUG


def parse(slug: str, q_label: str, root: Path) -> dict:
    """Read a Q<NN>.marking.md and parse it, regardless of which
    ASSESSMENTS the publish module points to."""
    p = root / f"{q_label}.marking.md"
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8")
    # Use the lower-level parser from publish directly. The
    # parse_question_marking() function reads ASSESSMENTS/<slug>/,
    # so we'll just inline the parse_criterion_block() call by
    # feeding the content into a hand-rolled equivalent.
    import re
    blocks = re.split(r"### Criterion \d+:\s*", text)
    criteria = []
    for block in blocks[1:]:
        c = fork_publish.parse_criterion_block(block)
        if c:
            criteria.append(c)
    m_avail = re.search(r"Total marks available:\s*(\d+)", text)
    total_avail = int(m_avail.group(1)) if m_avail else sum(c.get("marks_available", 0) for c in criteria)
    total_awarded = sum(c.get("marks_awarded", 0) for c in criteria)
    return {
        "q_label": q_label,
        "total_available": total_avail,
        "total_awarded": total_awarded,
        "criteria": criteria,
    }


def main() -> int:
    # Build a per-criterion table across both backends
    rows = []
    for qn in range(1, 10):
        ql = f"Q{qn:02d}"
        canon = parse(SLUG, ql, CANON)
        m3 = parse(SLUG, ql, FORK)
        if not canon or not m3:
            rows.append((qn, "?", "?", "?", "missing"))
            continue
        for i, (c_canon, c_m3) in enumerate(zip(canon["criteria"], m3["criteria"])):
            # Same number of criteria in both?
            if i >= len(canon["criteria"]) or i >= len(m3["criteria"]):
                rows.append((qn, f"c{i+1}", "?", "?", "criteria count mismatch"))
                continue
            subq = c_canon.get("subq", "?")
            d_canon = c_canon.get("decision", "?")
            d_m3 = c_m3.get("decision", "?")
            a_canon = c_canon.get("marks_awarded", 0)
            a_m3 = c_m3.get("marks_awarded", 0)
            avail = c_canon.get("marks_available", c_m3.get("marks_available", 0))
            if d_canon == d_m3 and a_canon == a_m3:
                tag = "same"
            elif d_canon != d_m3 and a_canon != a_m3:
                tag = "FLIP"  # both decision and marks differ
            elif d_canon != d_m3:
                tag = "flip"  # decision differs, same marks (rare)
            else:
                tag = "marks"  # same decision, different marks (rare)
            rows.append((qn, subq, f"{d_canon} {a_canon}/{avail}", f"{d_m3} {a_m3}/{avail}", tag))

    # Print as a markdown table
    print()
    print("| Q | Sub | Codex (canonical) | m3 (fork) | diff |")
    print("|---|---|---|---|---|")
    for r in rows:
        q, sub, c, m, tag = r
        if tag == "same":
            mark = "same"
        elif tag == "FLIP":
            mark = "FLIP"
        elif tag == "flip":
            mark = "decision flip"
        else:
            mark = "marks diff"
        print(f"| Q{q} | {sub} | {c} | {m} | {mark} |")

    # Per-question tallies
    print()
    print("**Per-question totals**")
    print()
    print("| Q | Codex | m3 | Δ |")
    print("|---|---:|---:|---:|")
    total_c = 0
    total_m = 0
    total_a = 0
    for qn in range(1, 10):
        ql = f"Q{qn:02d}"
        canon = parse(SLUG, ql, CANON)
        m3 = parse(SLUG, ql, FORK)
        if not canon or not m3:
            print(f"| Q{qn} | ? | ? | missing |")
            continue
        a_canon = canon["total_awarded"]
        a_m3 = m3["total_awarded"]
        a_avail = canon["total_available"]
        delta = a_m3 - a_canon
        sign = "+" if delta > 0 else ""
        print(f"| Q{qn} | {a_canon}/{a_avail} | {a_m3}/{a_avail} | {sign}{delta} |")
        total_c += a_canon
        total_m += a_m3
        total_a += a_avail
    print(f"| **TOT** | **{total_c}/{total_a}** | **{total_m}/{total_a}** | **{total_m - total_c:+d}** |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
