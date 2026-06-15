"""Quick test: drive discover_batch(engine='ollama-m3') end-to-end.

This exercises the same code path the orchestrator would, just
without the upstream wait_for_photos() and downstream restage.
"""
import sys
sys.path.insert(0, r'src')

from pathlib import Path
import discover_batch as db

# Pick the 26 most-recent photos, oldest-first (same as orchestrator)
photos = sorted(
    Path(r'C:\Users\openclaw-agent\.openclaw\media\inbound').glob('*.jpg'),
    key=lambda p: p.stat().st_mtime,
)[-26:]

print(f'using {len(photos)} photos', flush=True)
result = db.discover_batch(
    photo_paths=photos,
    job_name='m3-test-discover-2',
    yes=True,
    engine='ollama-m3',
)
print()
print('=== discover_batch result ===')
print(f'  slug: {result["slug"]}')
print(f'  cover_paper_code: {result["cover_paper_code"]}')
print(f'  cover_text: {result["cover_text"]}')
print(f'  page_order: {result["page_order"]}')
print(f'  confidence: {result["confidence"]}')
print(f'  page_numbers: {result["page_numbers"]}')
