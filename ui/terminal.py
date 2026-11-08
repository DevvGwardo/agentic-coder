"""
Agentic Coder Terminal — Modern Warp-inspired REPL.

Architecture:
  - All output to stderr via print() — simple, portable, Warp-compatible
  - No VT100 cursor games — just redraw what's needed on state changes
  - No readline conflicts — plain input() for the prompt
  - Clear screen + redraw header on init; incremental updates during runs
  - ANSI escape codes for colors and styling

Layout (top to bottom):
  - 4 rows: HERMES-CODE title banner in bordered panel
  - 1 row:  workspace: /path  v1.0  (inside info bar)
  - 1 row:  hint text
  - 1 row:  separator (─)
  - N-7 rows: activity feed (goal, phases, workers, results)
  - 1 row: status bar (git | host | user | tok | api | phase)
  - 1 row: prompt + input
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .browser import WorkspaceBrowser
from .commands import dispatcher


# ── ANSI helpers ──────────────────────────────────────────────────────────────

_CSI   = "\033["
_RESET = f"{_CSI}0m"
_BOLD  = f"{_CSI}1m"
_DIM   = f"{_CSI}2m"
_FG_RED= f"{_CSI}31m"
_FG_GRN= f"{_CSI}32m"
_FG_YLW= f"{_CSI}33m"
_FG_CYN= f"{_CSI}36m"
_FG_WHT= f"{_CSI}37m"
_FG_BRI= f"{_CSI}90m"


def _s(t: str)      -> str: return f"{_DIM}{t}{_RESET}"
def _b(t: str)      -> str: return f"{_BOLD}{t}{_RESET}"
def _cyan(t: str)   -> str: return f"{_FG_CYN}{t}{_RESET}"
def _green(t: str)  -> str: return f"{_FG_GRN}{t}{_RESET}"
def _yellow(t: str) -> str: return f"{_FG_YLW}{t}{_RESET}"
def _red(t: str)    -> str: return f"{_FG_RED}{t}{_RESET}"
def _gray(t: str)   -> str: return f"{_FG_BRI}{t}{_RESET}"


# ── OpenCode-inspired color palette ──────────────────────────────────────────
# Dark theme: warm tones, #0a0a0a background
# Colors use 256-color palette indices for consistency

_P = "\033[38;5;"    # foreground prefix
_B = "\033[48;5;"    # background prefix
_R = _RESET           # reset

_PAL_FG       = f"{_P}255m"   # #eeeeee  bright white
_PAL_MUTED    = f"{_P}240m"   # #808080  gray
_PAL_ORANGE   = f"{_P}215m"   # #fab283  warm orange (primary)
_PAL_PURPLE   = f"{_P}141m"   # #9d7cd8  purple (secondary)
_PAL_CYAN     = f"{_P}73m"    # #56b6c2  cyan (info)
_PAL_GREEN    = f"{_P}114m"   # #7fd88f  green (success)
_PAL_RED      = f"{_P}203m"   # #e06c75  red (error)
_PAL_YELLOW   = f"{_P}221m"   # #f5a742  yellow (warning)
_PAL_BG       = f"{_B}236m"   # #1a1a1a  panel background
_PAL_BG2      = f"{_B}239m"   # #303030  border/accent bg


# ── ANSI strip ────────────────────────────────────────────────────────────────

_ANSI_RE = __import__("re").compile(r"\033\[[0-9;]*[a-zA-Z]")


def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── Logo / Banner ─────────────────────────────────────────────────────────────

_VERSION = "1.0"

# Simple, clean banner that fits any terminal width
# "HERMES" in orange, "-CODE" in muted gray, all-caps
def _render_banner() -> list[str]:
    cols = shutil.get_terminal_size().columns
    inner = cols - 4  # account for │ padding

    title_left  = f"{_PAL_ORANGE}HERMES{_R}"
    title_right = f"{_PAL_MUTED}-CODE{_R}"
    title = f"{title_left}{title_right}"
    title_len = len("HERMES-CODE")

    # Decorative line: ▰ (alt-255 block) in orange, faded to muted
    filled = int((title_len / inner) * (inner - 2)) if inner > 2 else 0
    bar_len = max(0, inner - title_len - 4)
    bar_left  = bar_len // 2
    bar_right = bar_len - bar_left

    bar_line  = f"{_PAL_ORANGE}{'▰' * bar_left}{_PAL_MUTED}{'▱' * bar_right}{_R}"
    title_line = f"{' ' * 2}{bar_line}  {title}  {bar_line}{' ' * 2}"

    lines = [
        f"  {_PAL_BG2}┌{'─' * inner}┐{_R}",
        title_line,
        f"  {_PAL_BG2}└{'─' * inner}┘{_R}",
    ]
    return lines


# ── TerminalUI ────────────────────────────────────────────────────────────────

class TerminalUI:
    """
    Modern Warp-inspired REPL. Uses print() to stderr for all output.
    OpenCode dark theme aesthetic.
    """

    def __init__(
        self,
        workspace: str | Path,
        scratch_dir: Optional[str | Path] = None,
        max_workers: int = 3,
    ) -> None:
        self.workspace   = Path(workspace).resolve()
        self.scratch_dir = Path(scratch_dir) if scratch_dir else None
        self.max_workers = max_workers
        self.running     = True

        # Runtime state
        self.current_goal: str  = ""
        self.current_phase: str = "idle"
        self.workers_done: int = 0
        self.workers_total: int= 0
        self.api_calls: int    = 0
        self.tokens: int       = 0
        self.git_branch: str   = ""

        # Terminal
        self._rows, self._cols = self._terminal_size()

        # Sub-components
        self.browser   = WorkspaceBrowser(self.workspace)
        self.feed: list[str]   = []
        self._feed_height = 0

    # ── Terminal size ──────────────────────────────────────────────────────

    @staticmethod
    def _terminal_size() -> tuple[int, int]:
        try:
            sz = shutil.get_terminal_size(fallback=(80, 24))
            return sz.lines, sz.columns
        except Exception:
            return 24, 80

    # ── Output (all to stderr) ─────────────────────────────────────────────

    def _out(self, *parts: str) -> None:
        sys.stderr.write("".join(parts))
        sys.stderr.flush()

    def _print(self, *parts: str) -> None:
        sys.stderr.write("".join(parts) + "\n")
        sys.stderr.flush()

    # ── Static header ───────────────────────────────────────────────────────

    def _draw_header(self) -> None:
        cols = self._cols
        inner = cols - 4

        # Banner (3 lines)
        for line in _render_banner():
            self._print(line)

        # Workspace info bar
        version_tag = f"{_PAL_MUTED}v{_VERSION}{_R}"
        ws_str = f"workspace: {self.workspace}"
        info_bar = f"  │{_PAL_BG} {ws_str}  {version_tag}  │{_R}"
        self._print(info_bar)

        # Hint line
        hint = "type a goal and press enter  ·  /help for commands"
        self._print(f"  │{_PAL_MUTED} {hint} │{_R}")

        # Separator
        self._print(f"  │{_PAL_BG2}{'─' * inner}│{_R}")

    # ── Feed ────────────────────────────────────────────────────────────────

    def _max_feed_rows(self) -> int:
        # header: 3 banner + 1 ws + 1 hint + 1 sep = 6 rows
        # status bar at N-1, input at N
        return max(1, self._rows - 6 - 2)

    def _print_feed_line(self, line: str) -> None:
        stripped = _strip(line)
        feed_cols = self._cols - 4
        wrapped = max(1, (len(stripped) // feed_cols + 1))
        self._feed_height += wrapped

        self.feed.append(line)

        max_rows = self._max_feed_rows()
        while self._feed_height > max_rows and self.feed:
            removed = self.feed.pop(0)
            re_removed = _strip(removed)
            self._feed_height -= max(1, (len(re_removed) // feed_cols + 1))

        self._full_redraw()

    def _full_redraw(self) -> None:
        """Full screen redraw — simple, correct, no VT100 region bugs."""
        self._out(f"{_CSI}H{_CSI}2J")
        self._draw_header()
        self._draw_feed()
        self._draw_statusbar()
        self._draw_input_prompt()

    def _draw_feed(self) -> None:
        """Draw feed region only (no screen clear)."""
        max_rows = self._max_feed_rows()
        visible = self.feed[-max_rows:] if len(self.feed) > max_rows else self.feed
        for line in visible:
            self._print(f"  │ {line}")

    # ── Status bar ─────────────────────────────────────────────────────────

    def _build_status_bar(self) -> str:
        parts = []

        if self.git_branch:
            parts.append(f"{_PAL_ORANGE}{_R} {self.git_branch}")

        host = _get_hostname()
        user = _get_username()
        if host:
            parts.append(f"{_PAL_MUTED}{host}{_R}")
        if user:
            parts.append(f"{_PAL_MUTED}{user}{_R}")

        parts.append(f"{_PAL_BG2}·{_R}")
        parts.append(f"{_PAL_CYAN}tok{_R}:{self.tokens:,}")
        parts.append(f"{_PAL_CYAN}api{_R}:{self.api_calls}")

        phase = self.current_phase
        if phase and phase != "idle":
            parts.append(f" {_PAL_GREEN}●{_R} {phase}")
        else:
            parts.append(f" {_PAL_MUTED}○{_R} idle")

        content = "  ".join(parts)
        cols = self._cols
        content_len = len(_strip(content))
        padding = max(0, cols - content_len - 1)
        return f"{_PAL_BG}{_PAL_FG}{content}{' ' * padding}{_R}"

    def _draw_statusbar(self) -> None:
        rows = self._rows
        bar = self._build_status_bar()
        self._out(f"{_CSI}{rows - 1};1H")
        self._out(f"{_CSI}2K")
        self._out(bar + "\n")

    # ── Input prompt ────────────────────────────────────────────────────────

    def _draw_input_prompt(self) -> str:
        rows = self._rows
        cwd = self.browser.relpath(self.browser.cwd)
        cwd_disp = "~" if cwd == "." else cwd

        if self.current_phase and self.current_phase != "idle":
            phase_str = f"{_PAL_PURPLE}{self.current_phase}{_R}"
        else:
            phase_str = f"{_PAL_MUTED}idle{_R}"

        prompt = f"  {phase_str} {_PAL_MUTED}{cwd_disp}{_R} │ "
        prompt += f"{_PAL_GREEN}❯{_R} "

        self._out(f"{_CSI}{rows};1H")
        self._out(f"{_CSI}2K")
        self._out(prompt)
        return prompt

    # ── Full redraw ─────────────────────────────────────────────────────────

    def _redraw_all(self) -> None:
        self._full_redraw()

    # ── Public API ─────────────────────────────────────────────────────────

    def println(self, msg: str = "") -> None:
        self._print_feed_line(msg)

    def print_phase(self, phase: str, status: str = "") -> None:
        self.current_phase = phase
        self._print_feed_line(f"{_PAL_ORANGE}▸ {phase}{_R} {_s(status)}")
        # _full_redraw() already handled status bar

    def print_error(self, error: str) -> None:
        self._print_feed_line(f"  {_PAL_RED}✗{_R} {error}")

    def print_result(self, result: str) -> None:
        self._print_feed_line(f"  {_PAL_GREEN}✓{_R} {result}")

    # ── Main run loop ──────────────────────────────────────────────────────

    def run(self) -> None:
        self._out(f"{_CSI}?25l")
        try:
            self._update_git_branch()
            self._redraw_all()
            self._event_loop()
        finally:
            self._out(f"{_CSI}?25h")
            self._print()

    def _update_git_branch(self) -> None:
        try:
            import subprocess
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.workspace),
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                self.git_branch = result.stdout.strip()
        except Exception:
            self.git_branch = ""

    def _event_loop(self) -> None:
        rows = self._rows
        while self.running:
            self._draw_input_prompt()

            try:
                line = input("")
            except (KeyboardInterrupt, EOFError):
                self._print()
                break

            if not line.strip():
                continue

            if line.startswith("/"):
                self._out(f"{_CSI}{rows};1H{_CSI}2K")
                output = dispatcher.handle(line, self.browser, self)
                if output:
                    for ol in output.split("\n"):
                        self._print_feed_line(ol)
                # _print_feed_line calls _full_redraw which updates status bar
                continue

            self._out(f"{_CSI}{rows};1H{_CSI}2K")
            self._run_goal(line)
            self._update_git_branch()
            # _run_goal ends with idle phase, but since we don't call
            # _print_feed_line here, manually redraw
            self._full_redraw()

    # ── Engine runner ─────────────────────────────────────────────────────

    def _run_goal(self, goal: str) -> None:
        self.current_goal    = goal
        self.current_phase  = "initializing"
        self.workers_done   = 0
        self.workers_total  = 0
        self.api_calls      = 0
        self.tokens         = 0

        self._print_feed_line(f"{_PAL_ORANGE}▸ Goal:{_R} {goal}")

        if self.scratch_dir is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.scratch_dir = Path(f"/tmp/agentic-coder-{ts}")
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

        result_holder: dict = {}
        log_lines_seen: int = 0

        def run_engine():
            import io
            captured = io.StringIO()
            old = sys.stdout
            sys.stdout = captured
            try:
                from engine import AgenticCoder, AgenticCoderConfig
                config = AgenticCoderConfig(
                    workspace=str(self.workspace),
                    scratch_dir=str(self.scratch_dir),
                    max_workers=self.max_workers,
                    verbose=False,
                )
                coder = AgenticCoder(config)
                result = coder.run(goal)
                result_holder["result"] = result
                result_holder["error"]  = None
            except Exception as e:
                result_holder["error"] = e
            finally:
                sys.stdout = old
                result_holder["captured"] = captured.getvalue()

        thread = threading.Thread(target=run_engine, daemon=True)
        thread.start()

        while thread.is_alive():
            time.sleep(0.2)

            captured_text = result_holder.get("captured", "")
            new_lines = captured_text.splitlines()[log_lines_seen:]

            for raw in new_lines:
                line = raw.strip()
                if not line:
                    continue
                if "PHASE" in line:
                    phase_part = line.split("PHASE", 1)[-1].strip()
                    self.current_phase = phase_part.split(":")[0].strip()
                    self._print_feed_line(f"{_PAL_ORANGE}▸ PHASE{_R} {phase_part}")
                elif "Launched worker" in line:
                    self.workers_total += 1
                    self._print_feed_line(f"  {_PAL_YELLOW}›{_R} worker: {line.split('Launched worker', 1)[-1].strip()}")
                elif "Workers done" in line:
                    try:
                        done = line.split("Workers done:", 1)[-1].strip().split("/")[0].strip()
                        self.workers_done = int(done)
                    except ValueError:
                        pass
                    self._print_feed_line(f"  {_PAL_GREEN}✓{_R} workers done")
                elif "API calls" in line or "tokens" in line:
                    parts = line.replace(" ", "").split(",")
                    for p in parts:
                        if "APIcalls" in p:
                            try:
                                self.api_calls = int(p.split(":")[-1])
                            except ValueError:
                                pass
                        elif "tokens" in p:
                            try:
                                self.tokens = int(p.split(":")[-1])
                            except ValueError:
                                pass
                    self._print_feed_line(_s(f"  {line}"))
                elif "ERROR" in line or "Error" in line:
                    self._print_feed_line(f"  {_PAL_RED}✗{_R} {line}")
                else:
                    self._print_feed_line(_s(f"  {line}"))

            log_lines_seen = len(captured_text.splitlines())

        thread.join(timeout=2)

        captured_text = result_holder.get("captured", "")
        for raw in captured_text.splitlines()[log_lines_seen:]:
            if raw.strip():
                self._print_feed_line(_s(raw.strip()))

        if result_holder.get("error"):
            self._print_feed_line(f"  {_PAL_RED}✗ Error:{_R} {result_holder['error']}")
        elif "result" in result_holder:
            r = result_holder["result"]
            status_str = r.final_status.upper()
            color = _PAL_GREEN if r.final_status == "ready" else _PAL_YELLOW
            self._print_feed_line(f"  {color}▸ Final:{_R} {status_str}  {_s(r.scratch_dir)}")
            if r.spec:
                spec_lines = r.spec.strip().split("\n")[:15]
                for sl in spec_lines:
                    if sl.strip():
                        self._print_feed_line(_s("  " + sl[:self._cols - 4]))
                if len(r.spec) > 500:
                    self._print_feed_line(_s(f"  ... [{len(r.spec)} chars total]"))
        else:
            self._print_feed_line(_s("  No result returned."))

        self.current_goal  = ""
        self.current_phase = "idle"
        # final _print_feed_line will call _full_redraw to update status


# ── System helpers ─────────────────────────────────────────────────────────

def _get_hostname() -> str:
    try:
        import socket
        return socket.gethostname().split(".")[0]
    except Exception:
        return ""


def _get_username() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return os.getenv("USER", "")


# ── CLI entry point ─────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agentic Coder Terminal — OpenCode-inspired REPL")
    parser.add_argument("--workspace", "-w", required=True)
    parser.add_argument("--scratch-dir", "-s", default=None)
    parser.add_argument("--max-workers", "-n", type=int, default=3)
    args = parser.parse_args()

    ui = TerminalUI(
        workspace=args.workspace,
        scratch_dir=args.scratch_dir,
        max_workers=args.max_workers,
    )
    ui.run()


if __name__ == "__main__":
    main()
