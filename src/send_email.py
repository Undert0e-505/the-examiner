"""
src/send_email.py - render the per-assessment email to outbox/<batch>.txt,
                    and (in the auto-pipeline) actually send it via Gmail
                    SMTP using the app password in Credential Manager.

Like publish.py, this is a small, self-contained renderer. It
reads the marked assessment output (SUMMARY.md + Q*.marking.md)
plus private/<active>.json, and emits a plain-text email body
to outbox/<batch>.txt.

The script can either write the email to outbox/ (manual flow — the
user sends from Gmail themselves) or actually send it via Gmail
SMTP (auto-pipeline flow — the orchestrator calls send_email with
--send and a --to staging|live flag).

The active-identity safety rail (per the 2026-06-15 pivot):
  --to staging (default): email goes to the active identity's
    recipient_email_staging (always Aaron in practice). NO email
    goes to the student.
  --to live: email goes to the active identity's
    recipient_email_live. When active=aaron, this is also Aaron's
    address (so --to live is functionally a no-op while iterating).
    When active=will, --to live sends to WillJOakley@gmail.com
    WITH Aaron always cc'd (cc is unconditional on live).

The recipient_email_staging field is required. If it's still the
placeholder, the script refuses to run with a clear error pointing
to the file to edit.

Usage:

    # Manual flow (just render to outbox/):
    D:\\Python310\\python.exe src/send_email.py ^
        --batch aqa-84621h-chemistry-higher-2024-05 ^
        --yes

    # Auto flow (send via Gmail SMTP):
    D:\\Python310\\python.exe src/send_email.py ^
        --batch <slug> ^
        --send ^
        --to staging ^
        --yes
    D:\\Python310\\python.exe src/send_email.py ^
        --batch <slug> ^
        --send ^
        --to live ^
        --yes

    # Preview only (no file written, no email sent):
    D:\\Python310\\python.exe src/send_email.py --batch <slug> --dry-run

The text format is plain text (no HTML email), which is the
professional default and renders on every client.

Note: this file used to be named email.py. It was renamed to
send_email.py on 2026-06-15 because the stdlib `email` module
is imported by http.client and urllib.request, and a sibling
file named email.py shadows the stdlib module and breaks
imports of http.client / urllib.request elsewhere in the project
(notably the bucket self-healing in publish.py). send_email.py
is the unambiguous name.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import re
import smtplib
import ssl
import sys
from ctypes import wintypes
from email.message import EmailMessage  # stdlib; safe because we no longer shadow it
from pathlib import Path

from publish import (
    REPO_ROOT,
    read_student_json, parse_summary, parse_question_marking,
)

OUTBOX = REPO_ROOT / "outbox"


# ---------- Windows Credential Manager helper ----------

# These constants come from the Win32 API (wincred.h).
# We need them to call CredReadW via advapi32.
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", ctypes.c_uint64),  # actually a LARGE_INTEGER / FILETIME; c_uint64 works
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]

def _read_credential(target: str) -> str | None:
    """Read a Generic credential from Windows Credential Manager by
    target name. Returns the credential's blob as a string (UTF-8),
    or None if not found.

    Ctypes bindings for advapi32!CredReadW. The CredentialBlob is
    a length-prefixed byte array; we decode it as UTF-8.
    """
    advapi = ctypes.windll.advapi32
    cred_ptr = ctypes.c_void_p()
    # 0 = flags; CRED_TYPE_GENERIC = type
    ok = advapi.CredReadW(ctypes.c_wchar_p(target), CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr))
    if not ok:
        err = ctypes.GetLastError()
        # 1168 = ERROR_NOT_FOUND
        if err == 1168:
            return None
        raise OSError(f"CredReadW failed for {target!r}: Win32 error {err}")
    try:
        cred = ctypes.cast(cred_ptr, ctypes.POINTER(_CREDENTIAL)).contents
        if cred.CredentialBlobSize == 0:
            return ""
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        # Windows stores Generic credentials as UTF-16-LE if the
        # original was a non-empty Unicode string. Try UTF-16-LE
        # first; fall back to UTF-8 if the result has NULs that
        # don't decode cleanly.
        try:
            text = blob.decode("utf-16-le")
            # If round-tripping gives a clean string, use it.
            if text and not any(c == "\x00" for c in text):
                return text
        except UnicodeDecodeError:
            pass
        return blob.decode("utf-8", errors="replace")
    finally:
        advapi.CredFree(cred_ptr)


def get_gmail_app_password() -> str:
    """Read the Gmail app password from Windows Credential Manager.
    Stored under the target 'google-app-password:gmail' (per MEMORY.md).
    The first time this is called, Aaron runs the setup script at
    D:\\dev\\openclaw-scripts\\store-github-pat.ps1 (or a sibling that
    stores the Gmail app password under that target).
    """
    pw = _read_credential("google-app-password:gmail")
    if not pw:
        raise RuntimeError(
            "No Gmail app password in Windows Credential Manager under "
            "'google-app-password:gmail'. Aaron needs to store one before "
            "the auto-send path can run. See the setup instructions in "
            "MEMORY.md under 'My Google account'."
        )
    return pw


# ---------- Recipients + body (existing logic) ----------

def recipient(student: dict, mode: str) -> str:
    """Return the recipient email based on the --to flag.

    The active identity's `recipient_email_staging` and
    `recipient_email_live` fields are what we read. When the
    active identity is 'aaron' (default), both fields point at
    Aaron's email, so --to staging and --to live are equivalent
    — both go to Aaron. When the active identity is 'will',
    staging goes to Aaron (safety) and live goes to the student.
    """
    if mode == "staging":
        return student.get("recipient_email_staging", "")
    if mode == "live":
        return student.get("recipient_email_live", "")
    raise ValueError(f"unknown --to mode: {mode!r}; expected 'staging' or 'live'")


def subject_for(slug: str, summary: dict) -> str:
    total_a = summary.get("total_awarded", 0)
    total_p = summary.get("total_available", 0)
    pct = round(100 * total_a / total_p) if total_p else 0
    # Compact, scannable subject line. The slug is a content id, not
    # user-facing. Use the paper title from pair.json instead.
    return f"Your {summary.get('paper_code', 'GCSE')} result is ready — {total_a}/{total_p} ({pct}%)"


def render_email_body(slug: str, summary: dict, questions: list[dict], student: dict, public_url: str, mode: str) -> str:
    """Plain-text email. No markdown (most clients render it
    inconsistently). Tight prose, scannable, professional."""
    if mode == "staging":
        salutation = "Hi Aaron"
        first = ""
    else:
        salutation = student.get("salutation_for_email", "Hey")
        first = student.get("first_name_for_salutation", "")
    signoff = student.get("signoff_for_email", "")
    total_a = summary.get("total_awarded", 0)
    total_p = summary.get("total_available", 0)
    pct = round(100 * total_a / total_p) if total_p else 0

    # Per-Q tally
    q_lines = []
    for q in questions:
        qa = q.get("total_awarded", 0)
        qp = q.get("total_available", 0)
        qmark = "  " if qa == qp else ("+" if qa > 0.5 * qp else "-")
        q_lines.append(f"  {q['q_label']:<6} {qa:>3}/{qp:<3}   {qmark}")

    q_table = "\n".join(q_lines)

    # Where did the marks go? Pull the strongest 2-3 observations
    # from the SUMMARY (skip the OCR-blockers / IDK-pattern paragraphs
    # which are too operational for the student).
    obs = summary.get("observations_md", "").strip()
    obs_short = ""
    if obs:
        # Take the first 2 paragraphs only, capped to 600 chars
        paragraphs = [p for p in obs.split("\n\n") if p.strip()][:2]
        # Strip markdown for plain-text rendering
        obs_text = "\n\n".join(paragraphs)
        obs_text = re.sub(r"\*\*([^*]+)\*\*", r"\1", obs_text)   # bold
        obs_text = re.sub(r"`([^`]+)`", r"\1", obs_text)        # inline code
        if len(obs_text) > 800:
            obs_text = obs_text[:800].rsplit(".", 1)[0] + "..."
        obs_short = obs_text

    if first:
        salutation = f"{salutation} {first}"

    body = f"""{salutation},

Your {summary.get('paper_code', 'GCSE paper')} has been marked.

Total: {total_a}/{total_p} ({pct}%)

Per-question:
{q_table}

View the per-criterion breakdown on the assessment page:
{public_url}

{obs_short}

The page lets you agree or disagree with each mark, or add a
note about what you wrote on the page. The notes go into the
calibration log so future marks reflect what you've shown
you can do.

{signoff}
"""
    return body


# ---------- SMTP send ----------

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "jimothyoakley55@gmail.com"  # the Jimothy account, per MEMORY.md


def send_via_gmail(
    *,
    from_addr: str,
    to_addrs: list[str],
    cc_addrs: list[str],
    subject: str,
    body: str,
    app_password: str,
    timeout: int = 30,
) -> dict:
    """Send a plain-text email via Gmail SMTP using an app password.
    Returns a dict with `{'message_id': str, 'recipients': list[str]}`.

    All recipients (To + Cc) are passed to sendmail, which is what
    Gmail expects; the Cc header is also set explicitly so email
    clients render the cc. We do NOT include Bcc headers — the
    safety rail requires Aaron to be visible on live sends.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    if to_addrs:
        msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg.set_content(body)

    all_rcpts = list(to_addrs) + list(cc_addrs)
    if not all_rcpts:
        raise ValueError("send_via_gmail called with no recipients")

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=timeout) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ctx)
        smtp.ehlo()
        smtp.login(SMTP_USER, app_password)
        refused = smtp.sendmail(SMTP_USER, all_rcpts, msg.as_string())
    if refused:
        # smtp.sendmail returns a dict of {recipient: (code, error)} for
        # refused recipients; any entries mean at least one send failed.
        raise RuntimeError(f"Gmail refused some recipients: {refused}")
    return {
        "message_id": msg["Message-ID"],
        "recipients": all_rcpts,
    }


def resolve_recipients(student: dict, mode: str) -> tuple[str, list[str], list[str]]:
    """Return (to_primary, to_list, cc_list) for the given --to mode.

    The primary `to` is what's displayed as the visible recipient on
    the page header / message To: line. The cc list is what goes in
    Cc. The full recipient list is what's actually passed to Gmail's
    sendmail.

    Per Aaron's policy (2026-06-15): --to live is unconditional on
    Aaron being cc'd, even when active=will. Staging is never cc'd
    to anyone (it's already going to Aaron's own address).
    """
    if mode == "staging":
        staging = student.get("recipient_email_staging", "")
        if not staging:
            raise ValueError("recipient_email_staging is empty in private/<active>.json")
        return (staging, [staging], [])
    if mode == "live":
        live = student.get("recipient_email_live", "")
        staging = student.get("recipient_email_staging", "")
        if not live:
            raise ValueError("recipient_email_live is empty in private/<active>.json")
        cc = [staging] if staging and staging != live else []
        return (live, [live], cc)
    raise ValueError(f"unknown --to mode: {mode!r}; expected 'staging' or 'live'")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Render the per-assessment email and either write it to outbox/<batch>.txt "
            "(manual flow) or send it via Gmail SMTP (auto flow)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--batch", required=True, help="Paper slug (e.g. aqa-84621h-chemistry-higher-2024-05).")
    p.add_argument("--to", choices=("staging", "live"), default="staging",
                   help="Which recipient_email_* from private/<active>.json to use. Default staging (Aaron).")
    p.add_argument("--site", default="https://undert0e-505.github.io/the-examiner",
                   help="Base URL of the public Pages site. Default is the live URL.")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    p.add_argument("--dry-run", action="store_true", help="Don't write the file or send. Print the body.")
    p.add_argument(
        "--send",
        action="store_true",
        help="Actually send the email via Gmail SMTP. Without this flag, the email is "
             "written to outbox/<batch>.txt for manual review. The first time this is "
             "used in a new setup, a smoke-test to Aaron's own address is recommended "
             "before any --to live send to the student.",
    )
    p.add_argument(
        "--also-write-outbox",
        action="store_true",
        help="In addition to --send, also write the email body to outbox/<batch>.txt. "
             "Useful for the audit trail in the auto-pipeline.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    student = read_student_json(require_recipient=True)

    print(f"Active identity: {student['_active_identity']!r}  (from private/active.json)")
    print(f"Display name:    {student['display_name']!r}")

    slug = args.batch
    summary = parse_summary(slug)
    questions = []
    for q_row in summary.get("q_rows", []):
        q_short = q_row["q"]
        q_padded = "Q" + q_short.lstrip("Q").zfill(2)
        q = parse_question_marking(slug, q_padded)
        if q is not None:
            questions.append(q)
    questions.sort(key=lambda q: int(q["qnum"]))

    public_url = f"{args.site.rstrip('/')}/assessments/{slug}.html"
    subj = subject_for(slug, summary)
    body = render_email_body(slug, summary, questions, student, public_url, args.to)
    to_primary, to_list, cc_list = resolve_recipients(student, args.to)

    if args.dry_run:
        print(f"To:  {', '.join(to_list)}")
        if cc_list:
            print(f"Cc:  {', '.join(cc_list)}")
        print(f"Subject: {subj}")
        print("-" * 60)
        print(body)
        return 0

    # Write to outbox/ if --also-write-outbox (or if no --send at all).
    wrote_outbox = False
    if args.also_write_outbox or not args.send:
        OUTBOX.mkdir(parents=True, exist_ok=True)
        out = OUTBOX / f"{slug}.txt"
        if out.exists() and not args.yes:
            print(f"{out} already exists. Re-run with --yes to overwrite.")
            return 1
        cc_header = f"Cc: {', '.join(cc_list)}\n" if cc_list else ""
        out.write_text(
            f"To: {', '.join(to_list)}\n"
            f"{cc_header}"
            f"Subject: {subj}\n"
            f"\n"
            f"{body}",
            encoding="utf-8",
        )
        wrote_outbox = True
        print(f"  wrote {out}  ({len(body)} bytes body)")

    if not args.send:
        print(f"  To:      {', '.join(to_list)}")
        if cc_list:
            print(f"  Cc:      {', '.join(cc_list)}")
        print(f"  Subject: {subj}")
        if not wrote_outbox:
            print()
            print("Next step: review the file, then send from Gmail (or whichever client).")
        print("To switch from staging (Aaron) to live (student), re-run with --to live.")
        return 0

    # --send: actually send via Gmail SMTP.
    print(f"Sending to: {', '.join(to_list)}", flush=True)
    if cc_list:
        print(f"  cc:       {', '.join(cc_list)}", flush=True)
    print(f"  subject:  {subj}", flush=True)
    app_password = get_gmail_app_password()
    result = send_via_gmail(
        from_addr=SMTP_USER,
        to_addrs=to_list,
        cc_addrs=cc_list,
        subject=subj,
        body=body,
        app_password=app_password,
    )
    print(f"  sent. message-id: {result['message_id']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
