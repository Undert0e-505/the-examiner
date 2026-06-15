"""Test 2: OCR one page (Q4.5 mean temperature calc) on m3.

Compares the m3 transcript against the existing Codex transcript
that's already in the fork's intake dir.
"""
import sys
sys.path.insert(0, r'src')

from pathlib import Path
from backends.ollama_m3_ocr import ocr_photo

photo = Path(r'D:\dev\the-examiner-m3\intake\aqa-84621h-chemistry-higher-2024-05\12.jpg')
slug = 'aqa-84621h-chemistry-higher-2024-05'
page = 12

print(f'OCR-ing {photo.name} (printed page {page})...', flush=True)
text = ocr_photo(photo, slug, page, timeout=300.0)
print()
print('=== m3 transcript ===')
print(text)
print()
print('=== char count:', len(text))
