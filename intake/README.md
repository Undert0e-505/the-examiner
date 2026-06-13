# intake/

Photos from Telegram, one folder per batch. Each batch folder is named
by the timestamp you sent the photos (e.g. `2026-09-12T18-24/`).

```
intake/
└── 2026-09-12T18-24/
    ├── source.txt       # how this batch arrived (e.g. "Telegram, 7 photos")
    ├── 01.jpg
    ├── 02.jpg
    ├── ...
    └── 07.jpg
```

If a photo's paper cannot be confidently matched, it lands in
`intake/<batch>/unmatched/<n>.jpg` along with a short note in
`intake/<batch>/unmatched/NOTE.txt` so you (Aaron) can disambiguate.

Don't put anything in here by hand. Either send the photos to me on
Telegram (I'll file them) or use the `src/intake.py` helper for bulk
uploads.
