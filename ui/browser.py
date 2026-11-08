"""
WorkspaceBrowser — cd/ls/pwd for the REPL.
Maintains its own virtual CWD so the REPL can navigate independently
of the process working directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class WorkspaceBrowser:
    def __init__(self, root_path: str | Path) -> None:
        self.root = Path(root_path).resolve()
        self.cwd = self.root
        self._dir_stack: list[Path] = []

    # ── Navigation ───────────────────────────────────────────────────────────

    def pwd(self) -> str:
        return str(self.cwd)

    def cd(self, path: Optional[str] = None) -> str:
        """Change to path. None → root. Returns new cwd or error message."""
        if path is None:
            self.cwd = self.root
            return str(self.cwd)

        # ".." is allowed
        target = (self.cwd / path).resolve()

        # Stay within workspace root
        try:
            target.relative_to(self.root)
        except ValueError:
            return f"Access denied: {path} is outside workspace"

        if not target.exists():
            return f"No such directory: {path}"
        if not target.is_dir():
            return f"Not a directory: {path}"

        self._dir_stack.append(self.cwd)
        self.cwd = target
        return str(self.cwd)

    def pushd(self, path: str) -> str:
        """pushd alias — save current and cd."""
        return self.cd(path)

    def popd(self) -> str:
        """popd alias — restore previous directory."""
        if not self._dir_stack:
            return "Directory stack empty"
        self.cwd = self._dir_stack.pop()
        return str(self.cwd)

    # ── Listing ───────────────────────────────────────────────────────────────

    def ls(self, path: Optional[str] = None, long: bool = False) -> str:
        """List directory. Returns formatted string."""
        if path is None:
            target = self.cwd
        else:
            target = (self.cwd / path).resolve()

        if not target.exists():
            return f"No such file or directory: {path or '.'}"
        if not target.is_dir():
            return f"Not a directory: {path or '.'}"

        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError as e:
            return f"Permission denied: {e}"

        if long:
            return self._ls_long(entries)
        return self._ls_short(entries)

    def _ls_short(self, entries: list[Path]) -> str:
        if not entries:
            return ""
        lines = []
        for p in entries:
            icon = "📁 " if p.is_dir() else "📄 "
            lines.append(f"{icon}{p.name}")
        return "\n".join(lines)

    def _ls_long(self, entries: list[Path]) -> str:
        lines = []
        for p in entries:
            icon = "📁 " if p.is_dir() else "📄 "
            size = self._size_str(p.stat().st_size) if p.is_file() else "-"
            modified = self._format_time(p.stat().st_mtime)
            lines.append(f"{icon}{modified}  {size:>6}  {p.name}")
        return "\n".join(lines)

    def _size_str(self, size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.0f}{unit}"
            size /= 1024
        return f"{size:.0f}TB"

    def _format_time(self, timestamp: float) -> str:
        from datetime import datetime
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M")

    # ── Tree ─────────────────────────────────────────────────────────────────

    def tree(self, path: Optional[str] = None, max_depth: int = 2) -> str:
        """Print a depth-limited directory tree."""
        root = (self.cwd / (path or ".")).resolve()
        if not root.exists():
            return f"No such directory: {path or '.'}"

        lines = [str(root)]
        self._tree_recursive(root, prefix="", lines=lines, depth=0, max_depth=max_depth)
        return "\n".join(lines)

    def _tree_recursive(
        self, path: Path, prefix: str, lines: list[str], depth: int, max_depth: int
    ) -> None:
        if depth >= max_depth:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return

        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            branch = "└── " if is_last else "├── "
            icon = "📁 " if entry.is_dir() else "📄 "
            lines.append(f"{prefix}{branch}{icon}{entry.name}")
            if entry.is_dir():
                extension = "    " if is_last else "│   "
                self._tree_recursive(entry, prefix + extension, lines, depth + 1, max_depth)

    # ── Utilities ────────────────────────────────────────────────────────────

    def resolve(self, path: str) -> Path:
        """Resolve path relative to cwd."""
        return (self.cwd / path).resolve()

    def relpath(self, path: Path | str) -> str:
        """Path relative to workspace root."""
        try:
            return str(Path(path).relative_to(self.root))
        except ValueError:
            return str(path)

    def find_file(self, name: str, start: Optional[str] = None) -> list[str]:
        """Find all files matching name under start path."""
        root = (self.cwd / (start or ".")).resolve()
        if not root.exists():
            return []
        matches = []
        for p in root.rglob(name):
            matches.append(self.relpath(p))
        return matches
