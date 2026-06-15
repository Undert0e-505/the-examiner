"""
src/run_publish_and_email.py - thin orchestrator that runs publish.py
                                 and email.py in sequence.

For the common case of "render the page and stage the email for
review" you only want to call one script. This wraps both.

Usage:

    D:\\Python310\\python.exe src/run_publish_and_email.py ^        --batch aqa-84621h-chemistry-higher-2024-05 ^        --yes

    # all batches, staging recipient (Aaron):
    D:\\Python310\\python.exe src/run_publish_and_email.py --all --to staging --yes

    # preview only (publishes HTML to pages/, but prints email to
    # stdout instead of writing outbox/<batch>.txt):
    D:\\Python310\\python.exe src/run_publish_and_email.py --batch <slug> --dry-run

The script does NOT push to git (publish.py writes files, you
commit + push separately). It does NOT send the email
(email.py writes to outbox/, you send separately).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Reuse the publish and email scripts' main() functions.
import publish
import email as email_mod


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run publish.py + email.py for one or more batches.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--batch", help="One batch (paper slug).")
    g.add_argument("--all", action="store_true", help="Every batch with a SUMMARY.md.")
    p.add_argument("--to", choices=("staging", "live"), default="staging",
                   help="Recipient for the email. Default staging (Aaron).")
    p.add_argument("--site", default="https://undert0e-505.github.io/the-examiner",
                   help="Base URL of the public Pages site.")
    p.add_argument("--yes", action="store_true", help="Skip all confirmation prompts.")
    p.add_argument("--dry-run", action="store_true", help="Pass --dry-run through to both scripts.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.all:
        slugs = publish.discover_batches()
        if not slugs:
            print("No batches found.", file=sys.stderr)
            return 1
    else:
        slugs = [args.batch]

    # Phase 1: render HTML for each batch.
    print("=" * 60)
    print(f"Phase 1: render HTML for {len(slugs)} batch(es)")
    print("=" * 60)
    publish_argv = []
    if args.all:
        publish_argv += ["--all"]
    else:
        publish_argv += ["--batch", args.batch]
    if args.yes:    publish_argv += ["--yes"]
    if args.dry_run: publish_argv += ["--dry-run"]
    rc = publish.main(publish_argv)
    if rc != 0:
        print(f"publish.py exited with code {rc}; aborting.", file=sys.stderr)
        return rc

    # Phase 2: render email for each batch.
    print()
    print("=" * 60)
    print(f"Phase 2: render email for {len(slugs)} batch(es) ({args.to})")
    print("=" * 60)
    for slug in slugs:
        email_argv = ["--batch", slug, "--to", args.to, "--site", args.site]
        if args.yes:    email_argv += ["--yes"]
        if args.dry_run: email_argv += ["--dry-run"]
        rc = email_mod.main(email_argv)
        if rc != 0:
            print(f"email.py for {slug} exited with code {rc}; continuing with the next batch.", file=sys.stderr)

    print()
    print("=" * 60)
    print("Done.")
    print()
    print("Next steps:")
    print("  1. Open the rendered HTML at pages/assessments/<slug>.html in a browser.")
    print("     Check mobile (DevTools), desktop, and light/dark mode.")
    print("  2. Open the staged email at outbox/<slug>.txt.")
    print("  3. If everything looks right, commit + push:")
    print("       git add pages/ outbox/ src/")
    print('       git commit -m "publish: render assessment HTML + email for <slug>"')
    print("       git push origin main")
    print("  4. Send the email from Gmail (or whatever client you use).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
