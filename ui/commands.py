"""
CommandDispatcher — slash command registry and handler.

Commands are plain callables that receive (args: str, browser: WorkspaceBrowser, ui: TerminalUI).
They return None (pass through to LLM) or a string (display and suppress LLM).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional

from .browser import WorkspaceBrowser


# Each command is a callable with signature:
#   def cmd(args: str, browser: WorkspaceBrowser, ui: TerminalUI) -> str | None
# Return None to let the input pass through to the LLM.
# Return a string to display it and suppress LLM processing.
CommandFn = Callable[[str, WorkspaceBrowser, "TerminalUI"], Optional[str]]


@dataclass
class CommandDef:
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    fn: CommandFn = field(default=lambda *_: None)


class CommandDispatcher:
    def __init__(self) -> None:
        self._commands: dict[str, CommandDef] = {}
        self._aliases: dict[str, str] = {}  # alias → canonical name

    def register(
        self,
        name: str,
        description: str,
        aliases: tuple[str, ...] = (),
        args_hint: str = "",
    ) -> Callable[[CommandFn], CommandFn]:
        """Decorator to register a command."""

        def decorator(fn: CommandFn) -> CommandFn:
            cmd = CommandDef(name=name, description=description, aliases=aliases, args_hint=args_hint, fn=fn)
            self._commands[name] = cmd
            for alias in aliases:
                self._aliases[alias] = name
            return fn

        return decorator

    def resolve(self, name: str) -> Optional[CommandDef]:
        """Look up a command by name or alias."""
        if name in self._commands:
            return self._commands[name]
        if name in self._aliases:
            return self._commands[self._aliases[name]]
        return None

    def handle(self, line: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
        """Handle a line. Returns display string or None to pass to LLM."""
        if not line.startswith("/"):
            return None

        parts = line.lstrip("/").split(maxsplit=1)
        cmd_name = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        cmd = self.resolve(cmd_name)
        if cmd is None:
            return f"Unknown command: /{cmd_name}. Type /help for available commands."

        return cmd.fn(args, browser, ui)

    def help_text(self) -> str:
        """Generate /help output."""
        lines = ["Available commands:", ""]
        for cmd in sorted(self._commands.values(), key=lambda c: c.name):
            alias_str = f" (alias: {', '.join('/'+a for a in cmd.aliases)})" if cmd.aliases else ""
            args_str = f" {cmd.args_hint}" if cmd.args_hint else ""
            lines.append(f"  /{cmd.name}{args_str} — {cmd.description}{alias_str}")
        lines.append("")
        lines.append("Any non-slash input is sent to the LLM as a coding goal.")
        return "\n".join(lines)


# ── Global dispatcher (commands register themselves at import time) ──────────

dispatcher = CommandDispatcher()
help = dispatcher.register(name="help", description="Show available commands", aliases=("?",))


@dispatcher.register(name="status", description="Show current session status")
def cmd_status(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    lines = [
        f"Workspace:  {browser.pwd()}",
        f"Scratch:   {getattr(ui, 'scratch_dir', 'n/a')}",
        f"Goal:      {getattr(ui, 'current_goal', 'none')}",
        f"Phase:     {getattr(ui, 'current_phase', 'idle')}",
    ]
    return "\n".join(lines)


@dispatcher.register(name="ls", description="List directory contents", args_hint="[path]")
def cmd_ls(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    long = "-l" in (args or "")
    path = args.replace("-l", "").strip() if args else None
    return browser.ls(path=path, long=long)


@dispatcher.register(name="cd", description="Change directory", args_hint="<path>")
def cmd_cd(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    if not args.strip():
        return browser.cd(None)
    result = browser.cd(args.strip())
    return result


@dispatcher.register(name="pwd", description="Print working directory")
def cmd_pwd(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    return browser.pwd()


@dispatcher.register(name="tree", description="Show directory tree", args_hint="[path] [-d N]")
def cmd_tree(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    parts = args.split()
    path = None
    depth = 2
    for i, p in enumerate(parts):
        if p == "-d" and i + 1 < len(parts):
            try:
                depth = int(parts[i + 1])
            except ValueError:
                return f"Invalid depth: {parts[i+1]}"
        elif not p.startswith("-"):
            path = p
    return browser.tree(path=path, max_depth=depth)


@dispatcher.register(name="find", description="Find files by name", args_hint="<name>")
def cmd_find(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    if not args.strip():
        return "Usage: /find <filename>"
    matches = browser.find_file(args.strip())
    if not matches:
        return f"No files matching: {args}"
    return "\n".join(matches)


@dispatcher.register(name="clear", description="Clear the terminal screen")
def cmd_clear(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    ui.console.clear()
    return None  # Don't display anything


@dispatcher.register(name="exit", description="Exit the terminal", aliases=("quit", "q"))
def cmd_exit(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    ui.running = False
    return "Goodbye."


@dispatcher.register(name="diff", description="Show diff for a file", args_hint="<path>")
def cmd_diff(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    if not args.strip():
        return "Usage: /diff <file>"
    from pathlib import Path
    import subprocess

    file_path = browser.resolve(args.strip())
    if not file_path.exists():
        return f"File not found: {args}"

    # Try git diff for the file
    try:
        result = subprocess.run(
            ["git", "-C", str(browser.root), "diff", browser.relpath(file_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.stdout:
            return f"Changes in {args}:\n\n{result.stdout}"
        else:
            return f"No changes in: {args}"
    except Exception as e:
        return f"Could not get diff: {e}"


@dispatcher.register(name="log", description="Tail the agent log file", args_hint="[-n N]")
def cmd_log(args: str, browser: WorkspaceBrowser, ui: "TerminalUI") -> Optional[str]:
    import os
    from pathlib import Path

    log_dir = browser.root / "logs"
    if not log_dir.exists():
        return "No logs directory found."

    # Get most recent log
    log_files = sorted(log_dir.glob("session_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not log_files:
        return "No session logs found."

    n = 30
    parts = args.split()
    for i, p in enumerate(parts):
        if p == "-n" and i + 1 < len(parts):
            try:
                n = int(parts[i + 1])
            except ValueError:
                pass

    log_file = log_files[0]
    try:
        lines = log_file.read_text().splitlines()
        tail = lines[-n:]
        header = f"Log: {log_file.name} ({len(lines)} total lines)\n"
        return header + "\n".join(tail)
    except Exception as e:
        return f"Could not read log: {e}"
