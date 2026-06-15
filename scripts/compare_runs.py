"""
scripts/compare_runs.py — compare m3 run #1, #2, #3 against
canonical Codex. Per-question tallies and per-criterion diffs
across all three m3 runs.

Snapshots live at:
  snapshots/run_2026-06-15_21-07-37_#1/
  snapshots/run_2026-06-15_21-08-35_#2/
  snapshots/run_2026-06-15_21-28-27_#3/
The current run #3 is in assessments/ (overwriting the live
files; snapshot is the same data).

Run from D:\\dev\\the-examiner-m3.
"""
import re
import sys
from pathlib import Path

REPO = Path(r"D:\dev\the-examiner-m3")
SLUG = "aqa-84621h-chemistry-higher-2024-05"
CANON = Path(r"D:\dev\the-examiner\assessments") / SLUG
M3_RUNS = {
    1: REPO / "snapshots" / "run_2026-06-15_21-07-37" / "assessments",
    2: REPO / "snapshots" / "run_2026-06-15_21-28-27_#3",
    3: REPO / "snapshots" / "run_2026-06-15_21-53-51_#3-actual",
}


def parse(ql: str, root: Path) -> dict:
    p = root / f"{ql}.marking.md"
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8")
    blocks = re.split(r"### Criterion \d+:\s*", text)
    criteria = []
    for block in blocks[1:]:
        m = re.match(r"([^\n]+)", block)
        if not m:
            continue
        header = m.group(1).strip()
        ao_m = re.match(r"^(AO\d+)\s*(?:-+|—|–)\s*(\d+)\s*marks?", header)
        if not ao_m:
            continue
        marks_available = int(ao_m.group(2))
        m_sub = re.search(r"\*\*Sub-question this criterion applies to:\*\*\s*([^\n]+)", block)
        subq = m_sub.group(1).strip() if m_sub else "?"
        m_dec = re.search(r"\*\*Decision:\*\*\s*([A-Z_]+)", block)
        decision = m_dec.group(1).strip() if m_dec else "?"
        m_awd = re.search(r"\*\*Marks awarded:\*\*\s*(\d+)", block)
        marks_awarded = int(m_awd.group(1)) if m_awd else 0
        criteria.append({"subq": subq, "decision": decision, "marks_avail": marks_available, "marks_awarded": marks_awarded})
    m_avail = re.search(r"- Total marks available:\s*(\d+)", text)
    total_avail = int(m_avail.group(1)) if m_avail else sum(c["marks_avail"] for c in criteria)
    total_awarded = sum(c["marks_awarded"] for c in criteria)
    return {
        "q_label": ql,
        "total_available": total_avail,
        "total_awarded": total_awarded,
        "criteria": criteria,
    }


def main() -> int:
    # Per-Q tallies across all sources
    rows = []
    for qn in range(1, 10):
        ql = f"Q{qn:02d}"
        canon = parse(ql, CANON)
        runs = {n: parse(ql, p) for n, p in M3_RUNS.items()}
        row = {"q": qn, "canon": canon, "runs": runs}
        rows.append(row)

    # Tally table
    print("\n## Per-question tallies (m3 #1, #2, #3 + canonical Codex)\n")
    print("| Q | Codex (canonical) | m3 #1 | m3 #2 | m3 #3 | m3 mean |")
    print("|---|---:|---:|---:|---:|---:|")
    for r in rows:
        ca = r["canon"]["total_awarded"] if r["canon"] else 0
        cb = r["canon"]["total_available"] if r["canon"] else 0
        r1 = r["runs"][1]["total_awarded"] if r["runs"].get(1) else 0
        r2 = r["runs"][2]["total_awarded"] if r["runs"].get(2) else 0
        r3 = r["runs"][3]["total_awarded"] if r["runs"].get(3) else 0
        mean = (r1 + r2 + r3) / 3
        print(f"| Q{r['q']} | {ca}/{cb} | {r1} | {r2} | {r3} | {mean:.1f} |")

    # Totals row
    print("|---|---:|---:|---:|---:|---:|")
    sums = {1: 0, 2: 0, 3: 0}
    sums_c = 0
    sum_avail = 0
    for r in rows:
        for n in (1, 2, 3):
            if r["runs"].get(n):
                sums[n] += r["runs"][n]["total_awarded"]
        if r["canon"]:
            sums_c += r["canon"]["total_awarded"]
            sum_avail = r["canon"]["total_available"]
    mean_total = (sums[1] + sums[2] + sums[3]) / 3
    print(f"| **TOT** | **{sums_c}/100** | **{sums[1]}** | **{sums[2]}** | **{sums[3]}** | **{mean_total:.1f}** |")

    # Variance
    print()
    print("## Variance")
    print()
    range_m3 = max(sums.values()) - min(sums.values())
    print(f"- m3 across 3 runs: {sums[1]}, {sums[2]}, {sums[3]} (range: {range_m3}, mean: {mean_total:.1f})")
    print(f"- Codex: 70, 71, 70, 71, 70, 65 (your 6 runs; range: 6, mean: ~69.5)")

    # Per-criterion diff matrix
    print()
    print("## Per-criterion across m3 runs (only criteria that differ across any run)\n")
    print("| Q | Sub | Canon | m3#1 | m3#2 | m3#3 |")
    print("|---|---|---|---|---|---|")
    diff_count = 0
    for r in rows:
        if not r["canon"]:
            continue
        canon_crits = r["canon"]["criteria"]
        for i, c_canon in enumerate(canon_crits):
            run_strs = []
            differs = False
            for n in (1, 2, 3):
                rcs = r["runs"].get(n)
                if not rcs or i >= len(rcs["criteria"]):
                    run_strs.append("?")
                    differs = True
                    continue
                cm = rcs["criteria"][i]
                # Compare on marks_awarded (the actual delivered score)
                if cm["marks_awarded"] != c_canon["marks_awarded"]:
                    differs = True
                run_strs.append(f"{cm['decision'][:5]} {cm['marks_awarded']}/{cm['marks_avail']}")
            if differs:
                ca = f"{c_canon['decision'][:5]} {c_canon['marks_awarded']}/{c_canon['marks_avail']}"
                subq = c_canon["subq"]
                print(f"| Q{r['q']} | {subq} | {ca} | {run_strs[0]} | {run_strs[1]} | {run_strs[2]} |")
                diff_count += 1
    print()
    print(f"({diff_count} criteria differ across m3 runs OR vs canonical)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
