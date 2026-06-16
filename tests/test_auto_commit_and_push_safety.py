"""tests/test_auto_commit_and_push_safety.py

Regression test for the 2026-06-16 silent-push bug.

Bug: when the orchestrator ran on a worktree checked out on
`m3-adapter` instead of `main`, the `git push origin main` in
auto_commit_and_push() was a no-op (local `main` hadn't moved),
git returned "Everything up-to-date" with exit 0, and the
pipeline happily sent the email. The new 69/100 commit never
reached origin/main, so GitHub Pages kept serving the old
page.

Fix: push HEAD:refs/heads/main explicitly, then verify
origin/main moved to the new HEAD. Raise RuntimeError on
mismatch.

This test mocks git() to simulate the bug and the fix:

  1. test_push_uses_head_refs_heads_main:
     Confirms the push command is now
     `git push origin <sha>:refs/heads/main` instead of
     `git push origin main`. This is the literal command
     that closes the bug.

  2. test_silent_noop_push_is_detected:
     Simulates the exact 2026-06-16 bug. `git push` returns
     exit 0 ("Everything up-to-date"), but the mocked
     `git rev-parse origin/main` still returns the OLD sha
     (because in the buggy world, it never moved).
     The function MUST raise RuntimeError, not return
     silently.

  3. test_push_failure_is_loud:
     If `git push` itself returns a non-zero exit (auth,
     network, non-fast-forward), the function MUST raise.
     The old code raised CalledProcessError on check=True;
     the new code raises RuntimeError with the full output
     so it's actually readable in the orchestrator log.

  4. test_happy_path_advances_origin_main:
     The normal case: commit advances HEAD, push returns 0,
     origin/main returns the new sha. Function returns
     normally, prints the confirmation.

  5. test_no_commit_skips_push:
     If nothing was committed, don't push.

We do NOT need a real git repo for these. The test mocks
`run.git` so all subprocess calls are intercepted.
"""
from __future__ import annotations

import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

# Make src/ importable
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import run  # noqa: E402


def _cp(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess with the shape `git()` returns."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def _make_mock(*, head_moves: bool, push_returncode: int = 0,
               push_stdout: str = "", origin_main_sha: str = ""):
    """Build a fake_git function that simulates one orchestrator run.

    Args:
        head_moves: if True, the "commit" call bumps HEAD to new_sha.
                    If False, HEAD stays at old_sha (no-op commit).
        push_returncode: what `git push` returns (0 = silent success,
                         non-zero = real error).
        push_stdout: stdout from the push.
        origin_main_sha: what `git rev-parse origin/main` returns
                         AFTER the push. If empty, falls back to
                         the post-commit HEAD sha.
    """
    old_sha = "oldsha0000000000000000000000000"
    new_sha = "newsha1234567890abcdef"
    state = {"head_sha": old_sha}

    def fake_git(*args, **kwargs):
        if args[:2] == ("push", "origin"):
            return _cp(push_returncode, push_stdout)
        if args[0] == "rev-parse" and args[1] == "HEAD":
            return _cp(0, stdout=state["head_sha"])
        if args[:2] == ("rev-parse", "origin/main"):
            if origin_main_sha:
                return _cp(0, stdout=origin_main_sha)
            return _cp(0, stdout=state["head_sha"])
        if args[0] == "commit":
            if head_moves:
                state["head_sha"] = new_sha
            return _cp(0)
        if args[:2] == ("add",):
            return _cp(0)
        return _cp(0)

    return fake_git, old_sha, new_sha


# ---------- Test 1: the literal command changed ----------

def test_push_uses_head_refs_heads_main():
    """The fix: push command is now `git push origin <sha>:refs/heads/main`,
    not `git push origin main`. This is the one-line change that closes
    the silent-no-op class.
    """
    push_calls: list[tuple] = []
    fake_git, _, new_sha = _make_mock(head_moves=True)

    def spying_fake_git(*args, **kwargs):
        if args[:2] == ("push", "origin"):
            push_calls.append(args)
        return fake_git(*args, **kwargs)

    with patch.object(run, "git", side_effect=spying_fake_git):
        run.auto_commit_and_push(
            slug="test-slug", total_awarded=10, total_available=20
        )

    assert len(push_calls) == 1, f"expected exactly 1 push, got {len(push_calls)}"
    pushed_args = push_calls[0]
    # Must NOT be the old "git push origin main"
    assert pushed_args != ("push", "origin", "main"), (
        f"old broken push command still in use: {pushed_args}"
    )
    # Must be the new "git push origin <sha>:refs/heads/main"
    assert len(pushed_args) == 3, f"unexpected push shape: {pushed_args}"
    target = pushed_args[2]
    assert target.endswith(":refs/heads/main"), (
        f"push target should end with :refs/heads/main, got {target!r}"
    )
    sha = target.split(":")[0]
    assert sha == new_sha, (
        f"push should include the new HEAD sha {new_sha!r}, got {sha!r}"
    )


# ---------- Test 2: the 2026-06-16 bug is now caught ----------

def test_silent_noop_push_is_detected():
    """The exact 2026-06-16 failure mode: push returns 0
    ("Everything up-to-date"), but origin/main hasn't moved.
    The function MUST raise RuntimeError loudly.
    """
    new_sha = "newsha1234567890abcdef"
    old_sha = "oldsha1234567890abcdef"
    fake_git, _, _ = _make_mock(
        head_moves=True,
        push_returncode=0,
        push_stdout="Everything up-to-date",
        origin_main_sha=old_sha,  # the bug: origin/main did NOT move
    )

    with patch.object(run, "git", side_effect=fake_git):
        try:
            run.auto_commit_and_push(
                slug="test-slug", total_awarded=10, total_available=20
            )
        except RuntimeError as e:
            msg = str(e)
            assert "DID NOT MOVE" in msg, (
                f"error should reference the silent-failure class, got: {msg}"
            )
            assert new_sha in msg and old_sha in msg, (
                f"error should include both shas for debugging, got: {msg}"
            )
            return
    raise AssertionError(
        "auto_commit_and_push returned silently when origin/main did not move — "
        "this is the 2026-06-16 bug, regression!"
    )


# ---------- Test 3: push errors propagate loudly ----------

def test_push_failure_is_loud():
    """If `git push` itself returns non-zero (auth failure, network,
    non-fast-forward), the function must raise. Old code raised
    CalledProcessError via check=True; new code raises RuntimeError
    with the full stdout so it's readable.
    """
    fake_git, _, _ = _make_mock(
        head_moves=True,
        push_returncode=128,
        push_stdout=(
            "fatal: Authentication failed for "
            "https://github.com/Undert0e-505/the-examiner.git/"
        ),
    )

    with patch.object(run, "git", side_effect=fake_git):
        try:
            run.auto_commit_and_push(
                slug="test-slug", total_awarded=10, total_available=20
            )
        except RuntimeError as e:
            msg = str(e)
            assert "git push to origin/main failed" in msg, (
                f"error should identify the push failure, got: {msg}"
            )
            assert "Authentication failed" in msg, (
                f"error should include the git output for debugging, got: {msg}"
            )
            return
    raise AssertionError(
        "auto_commit_and_push returned silently when git push failed"
    )


# ---------- Test 4: happy path ----------

def test_happy_path_advances_origin_main():
    """The normal case: commit lands, push returns 0, origin/main
    moves to the new HEAD. Function returns normally, prints the
    confirmation line.
    """
    new_sha = "newsha1234567890abcdef"
    fake_git, _, _ = _make_mock(head_moves=True)

    buf = StringIO()
    with patch.object(run, "git", side_effect=fake_git):
        with redirect_stdout(buf):
            run.auto_commit_and_push(
                slug="test-slug", total_awarded=10, total_available=20
            )
    output = buf.getvalue()
    assert "origin/main confirmed at" in output, (
        f"expected confirmation print line, got: {output!r}"
    )
    assert new_sha[:8] in output, (
        f"confirmation should include the new sha prefix, got: {output!r}"
    )


# ---------- Test 5: no-op commit is handled gracefully ----------

def test_no_commit_skips_push():
    """If nothing was committed (HEAD didn't change), don't push.
    This happens when the orchestrator calls auto_commit_and_push
    but paths_to_add had no actual changes (e.g. publish was a
    no-op). The old code would still attempt a no-op push; the
    new code returns early.
    """
    push_calls: list[tuple] = []
    fake_git, _, _ = _make_mock(head_moves=False)  # commit is a no-op

    def spying_fake_git(*args, **kwargs):
        if args[:2] == ("push", "origin"):
            push_calls.append(args)
        return fake_git(*args, **kwargs)

    with patch.object(run, "git", side_effect=spying_fake_git):
        run.auto_commit_and_push(
            slug="test-slug", total_awarded=10, total_available=20
        )

    assert len(push_calls) == 0, (
        f"should not push when HEAD didn't move, but pushed: {push_calls}"
    )


# ---------- Test runner ----------

if __name__ == "__main__":
    tests = [
        test_push_uses_head_refs_heads_main,
        test_silent_noop_push_is_detected,
        test_push_failure_is_loud,
        test_happy_path_advances_origin_main,
        test_no_commit_skips_push,
    ]
    passed = 0
    failed = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(0 if failed == 0 else 1)
