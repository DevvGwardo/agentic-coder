"""Agentic Coder — Coordinator + MiniMax workers for Hermes.

Four-phase orchestration:
  1. Research   — parallel MiniMax subagent workers investigate the codebase
  2. Synthesis  — coordinator writes a spec from findings
  3. Implement  — parallel MiniMax subagent workers make targeted changes
  4. Verify     — coordinator validates changes against the spec

Each worker runs as a detached Python subprocess calling MiniMax directly.
Results are written to a shared scratch directory for the coordinator to read.
"""

from .engine import AgenticCoder, AgenticCoderConfig

__all__ = ["AgenticCoder", "AgenticCoderConfig"]
