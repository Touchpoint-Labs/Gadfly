"""The corrections queue — human edits to the builder's code, captured as compact
diffs and held for idle-time memory extraction.

Capture freezes the evidence (the builder's version vs. the human's) the moment a
divergence is noticed, because by extraction time the builder may have re-edited the
file. The extractor (a Stop-hook LLM call) reads the pending diffs, decides whether
any generalize into a durable memory, marks them processed, and clears the queue.

A correction is identified by (file, builder hash, human hash) — the builder version
that was changed AND what the human changed it to. Capture skips anything already
queued OR already processed, so the same human edit is never reprocessed (even after
the queue is cleared, and even though the edit-ledger stays purely builder-authored).
Keying on the builder hash too means a file the builder re-creates and the human
re-deletes is a distinct correction, not a false duplicate of the first deletion.

Queue: .gadfly/corrections.jsonl (append-only). Processed marker:
.gadfly/corrections_processed.json — {file: [recent "<builder>|<human>" keys]}.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROCESSED_CAP = 50  # recent correction keys kept per file (bounds the marker)


def _queue(gadfly_dir: Path) -> Path:
    return Path(gadfly_dir) / "corrections.jsonl"


def _processed_path(gadfly_dir: Path) -> Path:
    return Path(gadfly_dir) / "corrections_processed.json"


def _human_hash(after: Optional[str]) -> str:
    if after is None:
        return "deleted"
    return hashlib.sha256(after.encode("utf-8", "replace")).hexdigest()[:16]


def _key(builder_hash: Optional[str], human_hash: str) -> str:
    return f"{builder_hash or ''}|{human_hash}"


def _diff(file: str, before: str, after: Optional[str]) -> str:
    """A unified diff from the builder's version to the human's, or a deletion note."""
    if after is None:
        return f"# {file}: builder-authored file was DELETED by the human."
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"{file} (builder)", tofile=f"{file} (human)"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _processed(gadfly_dir: Path) -> dict:
    p = _processed_path(gadfly_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _seen(gadfly_dir: Path, file: str, builder_hash: Optional[str], human_hash: str) -> bool:
    """Already captured (still queued) or already extracted (processed) — either way
    this exact (file, builder version, human version) correction is not reprocessed."""
    key = _key(builder_hash, human_hash)
    if key in _processed(gadfly_dir).get(file, []):
        return True
    return any(c.get("file") == file
               and _key(c.get("builder_hash"), c.get("human_hash", "")) == key
               for c in pending(gadfly_dir))


def capture(gadfly_dir: Path, session: str, file: str, before: str,
            after: Optional[str], reason: str, builder_hash: Optional[str]) -> None:
    """Append one captured human correction, unless it's a no-op (no textual change)
    or already seen (queued/processed). `builder_hash` is the ledger's last recorded
    hash for the file — the builder version this correction diverged from."""
    diff = _diff(file, before, after)
    if not diff.strip():
        return
    human_hash = _human_hash(after)
    if _seen(gadfly_dir, file, builder_hash, human_hash):
        return
    p = _queue(gadfly_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "session": session, "file": file, "reason": reason,
            "builder_hash": builder_hash, "human_hash": human_hash, "diff": diff,
        }) + "\n")


def is_new_correction(gadfly_dir: Path, file: str, builder_hash: Optional[str],
                      after: Optional[str]) -> bool:
    """True if this (file, builder version, human version) correction is neither queued
    nor already processed — the same dedup test capture applies, exposed so a hot-path
    pre-check can tell 'real new correction' from a file that merely stays diverged
    forever (the ledger never advances past a human edit, so diverged() keeps it)."""
    return not _seen(gadfly_dir, file, builder_hash, _human_hash(after))


def pending(gadfly_dir: Path) -> list[dict]:
    """All captured corrections awaiting extraction (oldest first)."""
    return _read_jsonl(_queue(gadfly_dir))


def mark_processed(gadfly_dir: Path, items: list[dict]) -> None:
    """Record (file → correction key) for consumed corrections, so the same human
    edit is never re-captured after the queue is cleared (capped per file)."""
    proc = _processed(gadfly_dir)
    for c in items:
        file = c.get("file")
        if not file:
            continue
        key = _key(c.get("builder_hash"), c.get("human_hash", ""))
        keys = proc.setdefault(file, [])
        if key not in keys:
            keys.append(key)
        proc[file] = keys[-_PROCESSED_CAP:]
    p = _processed_path(gadfly_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(proc))


def clear(gadfly_dir: Path) -> None:
    """Drop the queue once extraction has consumed (and marked) it."""
    try:
        _queue(gadfly_dir).unlink()
    except OSError:
        pass
