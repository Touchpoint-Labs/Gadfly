"""The edit-ledger — every executed AI edit, recorded post-execution.

A PostToolUse hook appends one record per builder Write/Edit: the file, a content
hash, and a snapshot of the content the edit produced. This is Gadfly's authorship
record and the basis of its feedback loop — a file in the tree that never appears
here is human-authored, and a tracked file whose CURRENT content no longer matches
the builder's last snapshot was changed out-of-band (a human corrected the builder's
code) since. The snapshot is what lets that human change be diffed against the
builder's version and handed to memory extraction; the ledger itself does no LLM
work.

Append-only JSONL at .gadfly/edits.jsonl; content snapshots (latest per file) at
.gadfly/snapshots/. Paths are stored absolute + resolved so comparison is stable.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _hash_file(path: Path) -> Optional[str]:
    """Fingerprint of a file's current bytes, or None if it's gone/unreadable."""
    try:
        return _digest(path.read_bytes())
    except OSError:
        return None


class EditLedger:
    def __init__(self, gadfly_dir: Path):
        self.dir = Path(gadfly_dir)
        self.path = self.dir / "edits.jsonl"

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn line (killed mid-write) loses one record, not the ledger
        return out

    def _latest(self) -> dict[str, str]:
        """The last recorded hash per file, in one pass (later records win)."""
        latest: dict[str, str] = {}
        for r in self._read():
            f = r.get("file")
            if f:
                latest[f] = r.get("hash")
        return latest

    def _snap_path(self, resolved_file: str) -> Path:
        slug = hashlib.sha256(resolved_file.encode()).hexdigest()[:24]
        return self.dir / "snapshots" / slug

    def record(self, session: str, tool: str, file: str) -> None:
        """Append the post-edit state of `file` and snapshot its content. Called from
        PostToolUse, after the tool ran — so it captures exactly what the builder's
        edit produced: a hash (for cheap divergence checks) and the full content (so a
        later human edit of the same file can be diffed against the builder's version).
        Out-of-workspace edits (e.g. /tmp scratch files) aren't project authorship and are
        skipped — otherwise a scratch file the OS later cleans up reads as a human 'deletion'
        (feeding the extractor) and inflates codemap-staleness. A file with nothing on disk to
        fingerprint is skipped."""
        p = Path(file).resolve()
        if not p.is_relative_to(self.dir.resolve().parent):
            return
        try:
            content = p.read_bytes()
        except OSError:
            return
        snap = self._snap_path(str(p))
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_bytes(content)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "session": session, "tool": tool, "file": str(p), "hash": _digest(content),
            }) + "\n")

    def last_hash(self, file: str) -> Optional[str]:
        """The most recently recorded hash for a file (None if never AI-touched)."""
        return self._latest().get(str(Path(file).resolve()))

    def last_content(self, file: str) -> Optional[str]:
        """The builder's last-snapshotted content of a file (None if untracked or the
        snapshot is unreadable) — the 'before' a human edit is diffed against."""
        try:
            return self._snap_path(str(Path(file).resolve())).read_text()
        except (OSError, UnicodeDecodeError):
            return None

    def tracked_files(self) -> list[str]:
        """Every file the builder has touched, in first-seen order."""
        return list(self._latest())

    def edits_since(self, mtime: float, *, exclude_suffixes: tuple[str, ...] = (".md",)) -> int:
        """How many builder edits were recorded after `mtime` (epoch seconds), skipping files
        with an excluded suffix. Used to gauge codemap staleness — docs (.md) don't count as
        structural change."""
        n = 0
        for r in self._read():
            f = r.get("file", "")
            if f.endswith(exclude_suffixes):
                continue
            try:
                when = datetime.fromisoformat(r.get("ts", "")).timestamp()
            except ValueError:
                continue
            if when > mtime:
                n += 1
        return n

    def diverged(self) -> list[dict]:
        """Tracked files whose CURRENT disk content no longer matches the builder's
        last recorded edit — changed out-of-band since. Each: {file, reason}, where
        reason is 'modified' (different content present) or 'deleted' (gone)."""
        out: list[dict] = []
        for file, recorded in self._latest().items():
            current = _hash_file(Path(file))
            if current == recorded:
                continue
            out.append({"file": file, "reason": "deleted" if current is None else "modified"})
        return out

    def divergence(self, file: str) -> Optional[str]:
        """Why one file is out of sync with the builder's last edit ('modified' /
        'deleted'), or None if it matches or was never builder-touched — i.e. whether a
        human changed it. The cheap per-file check the feedback capture uses."""
        recorded = self.last_hash(file)
        if recorded is None:
            return None
        current = _hash_file(Path(file).resolve())
        if current == recorded:
            return None
        return "deleted" if current is None else "modified"

    def reset(self) -> None:
        """Drop all authorship records + snapshots — current disk becomes the new baseline.
        `gadfly enable` calls this after a disabled window so builder edits made while Gadfly
        was off aren't later read as human corrections."""
        try:
            self.path.unlink()
        except OSError:
            pass
        shutil.rmtree(self.dir / "snapshots", ignore_errors=True)
