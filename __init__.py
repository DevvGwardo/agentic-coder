"""Agentic Coder — Coordinator + Claude Code workers for Hermes.

Four-phase orchestration:
  1. Research   — parallel Claude Code workers investigate the codebase
  2. Synthesis  — coordinator writes a spec from findings
  3. Implement  — parallel Claude Code workers make targeted changes
  4. Verify     — workers validate changes against the spec

Each worker runs as a detached Claude Code process. Results are written
to a shared scratch directory for the coordinator to read.
"""

from .engine import AgenticCoder, AgenticCoderConfig

__all__ = ["AgenticCoder", "AgenticCoderConfig"]
