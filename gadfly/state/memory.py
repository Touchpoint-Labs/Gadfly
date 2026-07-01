"""Reads the supervised project's narrative memory files, and workspace files for
surrounding-code context.

Missing files read as empty so supervisors degrade gracefully before a project has
written them. Reads are fresh on each access — these files change as the build
proceeds. (The decisions ledger and the global learned-memory store are their own
modules — see decisions.py and learned.py.)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


class ProjectMemory:
    def __init__(self, workspace: Path):
        self.root = Path(workspace)

    def path_for(self, name: str) -> Path:
        """The on-disk path for a managed file, matching an existing file's case
        (claude.md / CLAUDE.md) so writes land in it rather than a second file; an
        exact-case match wins, else the given name at the workspace root."""
        exact = self.root / name
        if exact.is_file():
            return exact
        want = name.lower()
        try:
            for p in self.root.iterdir():
                if p.is_file() and p.name.lower() == want:
                    return p
        except OSError:
            pass
        return exact

    def _read(self, name: str) -> str:
        try:
            return self.path_for(name).read_text()
        except OSError:
            return ""

    @property
    def spec(self) -> str:
        return self._read("spec.md")

    @property
    def claude(self) -> str:
        return self._read("claude.md")

    @property
    def codemap(self) -> str:
        return self._read("codemap.md")

    def file_around(self, target: Optional[str], anchor: Optional[str] = None,
                    window: int = 8000) -> str:
        """Current content of a workspace file for context: the whole file if
        small, else a window around `anchor` (the snippet being changed). Accepts
        absolute paths (as hooks provide) or workspace-relative ones."""
        if not target:
            return ""
        p = Path(target)
        if not p.is_absolute():
            p = self.root / target
        try:
            text = p.read_text()
        except OSError:
            return ""
        if len(text) <= window:
            return text
        if anchor and anchor in text:
            i = text.index(anchor)
            start = max(0, i - window // 2)
            return text[start:start + window]
        return text[:window]
