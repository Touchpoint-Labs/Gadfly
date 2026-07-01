"""Per-session rolling conversation digests.

A supervisor reads a small, stable summary plus a bounded recent tail. Older turns
fold into the digest recursively (digest_new = summarize(digest_old, overflow)),
so old context survives compressed rather than scrolling off the window. The LLM
summarizer is injected; this module only decides what to fold and writes state.

State per session:
  .gadfly/digests/<session>.md
  .gadfly/digests/<session>.json ({"folded": N})
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

DEFAULT_TAIL_BUDGET = 40000  # chars (~10k tokens): the verbatim tail's cap
DEFAULT_KEEP_FRACTION = 0.25


def session_slug(session: str) -> str:
    """Filesystem-safe session id for digest/lock filenames."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", session).strip("._")
    return slug or "unknown"


def _digest_dir(gadfly_dir: Path) -> Path:
    return Path(gadfly_dir) / "digests"


def _digest_path(gadfly_dir: Path, session: str) -> Path:
    return _digest_dir(gadfly_dir) / f"{session_slug(session)}.md"


def _meta_path(gadfly_dir: Path, session: str) -> Path:
    return _digest_dir(gadfly_dir) / f"{session_slug(session)}.json"


def read(gadfly_dir: Path, session: str) -> str:
    """The current digest for this session (empty if none yet)."""
    try:
        return _digest_path(gadfly_dir, session).read_text()
    except OSError:
        return ""


def folded(gadfly_dir: Path, session: str) -> int:
    """How many convo records are already folded into this session's digest."""
    p = _meta_path(gadfly_dir, session)
    if not p.is_file():
        return 0
    try:
        data = json.loads(p.read_text())
        return int(data.get("folded", 0)) if isinstance(data, dict) else 0
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _write(gadfly_dir: Path, session: str, digest_text: str, folded_n: int) -> None:
    d = Path(gadfly_dir)
    _atomic_write(_digest_path(d, session), digest_text)
    _atomic_write(
        _meta_path(d, session), json.dumps({"session": session, "folded": folded_n})
    )


def _convo(store, session: str) -> list[dict]:
    return [r for r in store.records(session) if r.get("t") == "convo"]


def _render(records: list[dict]) -> str:
    out = []
    for r in records:
        kind = r.get("kind", "text")
        tag = (
            f"{r.get('role', '')} (thinking)"
            if kind == "thinking"
            else r.get("role", "")
        )
        out.append(f"[{tag}] {r.get('text', '')}")
    return "\n".join(out)


def _keep_count(records: list[dict], budget: int) -> int:
    """How many trailing records fit in `budget` chars (at least one)."""
    used, keep = 0, 0
    for r in reversed(records):
        used += len(r.get("text", ""))
        if used > budget and keep > 0:
            break
        keep += 1
    return keep


def tail(
    store, session: str, gadfly_dir: Path, *, max_chars: int | None = None
) -> list[dict]:
    """The verbatim convo records not yet folded into the digest, thinking excluded.

    With max_chars, return the newest whole records under that budget (at least one
    if the unfolded tail is non-empty). This keeps review context bounded even when
    the async compactor has not caught up.
    """
    records = [
        r for r in _convo(store, session)[folded(gadfly_dir, session):]
        if r.get("kind") != "thinking"
    ]
    if max_chars is None:
        return records
    keep = _keep_count(records, max_chars)
    return records[-keep:] if keep else []


def needs_compaction(
    store, session: str, gadfly_dir: Path, *, budget: int = DEFAULT_TAIL_BUDGET
) -> bool:
    region = _convo(store, session)[folded(gadfly_dir, session) :]
    return sum(len(r.get("text", "")) for r in region) > budget


def compact(
    store,
    session: str,
    gadfly_dir: Path,
    summarize: Callable[[str, str], str],
    *,
    budget: int = DEFAULT_TAIL_BUDGET,
    keep_fraction: float = DEFAULT_KEEP_FRACTION,
) -> bool:
    """Fold old overflow into the per-session digest once the tail exceeds budget.

    Keeps roughly `keep_fraction` of the budget as unfolded recent context. Returns
    True if it compacted.
    """
    folded_n = folded(gadfly_dir, session)
    region = _convo(store, session)[folded_n:]
    if sum(len(r.get("text", "")) for r in region) <= budget:
        return False
    keep_budget = max(1, int(budget * keep_fraction))
    keep = _keep_count(region, keep_budget)
    to_fold = len(region) - keep
    if to_fold <= 0:
        return False
    new_digest = summarize(read(gadfly_dir, session), _render(region[:to_fold]))
    _write(gadfly_dir, session, new_digest, folded_n + to_fold)
    return True
