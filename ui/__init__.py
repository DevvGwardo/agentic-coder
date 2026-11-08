"""
agentic_coder.ui — OpenCode-style terminal interface.

Components:
    TerminalUI   — main REPL loop with rich output
    WorkspaceBrowser — cd/ls/pwd within the project
    CommandDispatcher — /help, /status, /diff, /clear, /exit ...
"""

from __future__ import annotations

# Support running as:
#   python -m ui.main --workspace .
# (when cwd is the agentic_coder directory)
try:
    from .terminal import TerminalUI
    from .browser import WorkspaceBrowser
    from .commands import CommandDispatcher
except ImportError:
    # Fallback for: python ui/main.py
    from terminal import TerminalUI
    from browser import WorkspaceBrowser
    from commands import CommandDispatcher

__all__ = ["TerminalUI", "WorkspaceBrowser", "CommandDispatcher"]
