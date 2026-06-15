"""One-off script: rewrite 'Will' narrations in gitignored
assessment files to 'the student' / 'You' for second-person feedback.

Run once. Idempotent.
"""
import re
from pathlib import Path

ROOT = Path(r"D:\dev\the-examiner\assessments\aqa-84621h-chemistry-higher-2024-05")


def rewrite(text: str) -> str:
    """Walk the text, treating runs of text in `"`, `'`, or `` ` ``
    as opaque (verbatim-quote zones we don't touch), and applying
    the Will -> you/the student substitution to the rest."""
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
        # Accumulate non-quote text
        j = i
        while j < n and text[j] not in ('"', "'", "`"):
            j += 1
        seg = text[i:j]
        # Possessive: Will's -> the student's
        seg = re.sub(r"\bWill's\b", "the student's", seg)
        # Plain Will in narration -> You (second-person feedback)
        seg = re.sub(r"\bWill\b", "You", seg)
        out.append(seg)
        i = j
    return "".join(out)


def main() -> int:
    files = sorted(ROOT.glob("*.md"))
    print(f"Found {len(files)} files")
    for f in files:
        before = f.read_text(encoding="utf-8")
        after = rewrite(before)
        if before != after:
            f.write_text(after, encoding="utf-8")
            # Count approximate line diffs
            n_before = before.count("Will")
            n_after = after.count("Will")
            print(f"  {f.name}: 'Will' count {n_before} -> {n_after}")
        else:
            print(f"  {f.name}: no change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
