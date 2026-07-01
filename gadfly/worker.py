"""Background maintenance workers — digest compaction and feedback extraction.

Both run outside the PreToolUse verdict path: hooks nudge them fire-and-forget under
a per-session lock, so review context stays bounded and human corrections get learned
even if a worker lags.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import feedback
from .config import load
from .factory import build_extractor, build_provider, build_store
from .providers.llm import LLMTransientError
from .state import digest
from .supervisors import make_summarizer


def _lock_path(gadfly_dir: Path, session: str, kind: str = "digest") -> Path:
    return Path(gadfly_dir) / "locks" / f"{kind}-{digest.session_slug(session)}.lock"


def _claim_lock(path: Path) -> int | None:
    """Acquire the per-session lock, or None if another LIVE worker holds it. The lock is an
    advisory flock: the kernel releases it when the owner exits — even on SIGKILL — so a
    worker killed past its cleanup can never wedge the session's maintenance forever. The
    lock file is left in place on release (unlinking would race a waiter); its content is
    just the owner pid, for debugging."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:                      # held by a live worker
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def compact_session(
    workspace: Path,
    session: str,
    summarize: Callable[[str, str], str],
    *,
    budget: int = digest.DEFAULT_TAIL_BUDGET,
) -> bool:
    """Compact one workspace/session if its unfolded tail is over budget.

    Returns True when compaction wrote a new digest. If another worker already owns
    the session lock, returns False.
    """
    store = build_store(workspace)
    lock = _lock_path(store.gadfly_dir, session)
    fd = _claim_lock(lock)
    if fd is None:
        return False
    try:
        return digest.compact(
            store, session, store.gadfly_dir, summarize, budget=budget
        )
    finally:
        os.close(fd)  # releases the flock; the lock file is left in place


def feedback_pass(workspace: Path, session: str, extractor) -> bool:
    """One idle-time feedback pass under a per-session lock: reconcile human out-of-band
    edits into the corrections queue, then extract any that generalize into durable
    rules. Returns False when another pass already holds the lock (it does the work)."""
    store = build_store(workspace)
    gadfly_dir = store.gadfly_dir
    lock = _lock_path(gadfly_dir, session, "feedback")
    fd = _claim_lock(lock)
    if fd is None:
        return False
    try:
        feedback.reconcile(gadfly_dir, session)
        feedback.run_extraction(extractor, workspace=Path(workspace), gadfly_dir=gadfly_dir)
        return True
    finally:
        os.close(fd)  # releases the flock; the lock file is left in place


def start_digest_worker(workspace: Path, session: str) -> None:
    """Nudge a one-shot digest worker without waiting for it."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gadfly.worker",
            "digest",
            "--workspace",
            str(workspace),
            "--session",
            session,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env={**os.environ, "GADFLY_HOOK_DISABLED": "1"},
    )


def maybe_start_digest_worker(workspace: Path, session: str) -> None:
    """Start a worker only when the session tail is oversized. Best-effort."""
    try:
        store = build_store(workspace)
        if digest.needs_compaction(store, session, store.gadfly_dir):
            start_digest_worker(workspace, session)
    except Exception:
        pass


def start_feedback_worker(workspace: Path, session: str) -> None:
    """Nudge a one-shot feedback worker without waiting for it."""
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gadfly.worker",
            "feedback",
            "--workspace",
            str(workspace),
            "--session",
            session,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env={**os.environ, "GADFLY_HOOK_DISABLED": "1"},
    )


def maybe_start_feedback_worker(workspace: Path, session: str) -> None:
    """Start a feedback worker only when a human correction is actually pending — the
    dedup-aware gate, so a file that stays diverged forever never re-spawns it. Best-effort."""
    try:
        store = build_store(workspace)
        if feedback.has_pending_work(store.gadfly_dir):
            start_feedback_worker(workspace, session)
    except Exception:
        pass


def _run_digest(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    config = load(workspace / "gadfly.toml")
    provider = build_provider(config)
    summarize = make_summarizer(provider, config.models.code, attempts=1)
    last: LLMTransientError | None = None
    for _ in range(max(1, config.llm_retries)):
        try:
            compact_session(workspace, args.session, summarize)
            return 0
        except LLMTransientError as e:
            last = e
    if last is not None:
        raise last
    return 0


def _run_feedback(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace)
    config = load(workspace / "gadfly.toml")
    extractor = build_extractor(config, build_provider(config))
    feedback_pass(workspace, args.session, extractor)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gadfly.worker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest")
    d.add_argument("--workspace", required=True)
    d.add_argument("--session", required=True)
    d.set_defaults(func=_run_digest)
    f = sub.add_parser("feedback")
    f.add_argument("--workspace", required=True)
    f.add_argument("--session", required=True)
    f.set_defaults(func=_run_feedback)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
