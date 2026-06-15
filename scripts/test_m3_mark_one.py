"""Test 3: mark Q1 on m3.

Q1 is a 10-marker, 8 criteria (AO2 + AO1 + AO1 + AO1 + AO1 + AO2 + AO1 + AO3),
spans pages 2-4 (sub-questions Q01.1-Q01.6). Good test case.
"""
import sys
sys.path.insert(0, r'src')

from backends.ollama_m3_mark import mark_batch

written = mark_batch(
    slug='aqa-84621h-chemistry-higher-2024-05',
    questions_to_mark=[1],
)
print()
print(f'wrote {len(written)} marking file(s):')
for p in written:
    print(f'  {p}')
