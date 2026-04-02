#!/usr/bin/env python3
"""Agentic Coder CLI — run from terminal or cron."""

import argparse
import os
import sys
import json
from pathlib import Path

# Add this dir to path
sys.path.insert(0, str(Path(__file__).parent))

from engine import AgenticCoder, AgenticCoderConfig


def main():
    parser = argparse.ArgumentParser(description="Agentic Coder — Coordinator + Claude Code workers")
    parser.add_argument("goal", help="The coding task to accomplish")
    parser.add_argument("--workspace", "-w", required=True, help="Codebase directory")
    parser.add_argument("--scratch-dir", "-s", default=None, help="Scratch directory (auto-generated if omitted)")
    parser.add_argument("--max-workers", "-n", type=int, default=3, help="Max parallel workers (default: 3)")
    parser.add_argument("--verbose", "-v", action="store_true", default=True, help="Verbose output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress logs")
    parser.add_argument("--timeout", "-t", type=int, default=10, help="Timeout per worker in minutes (default: 10)")
    parser.add_argument("--output", "-o", default=None, help="Write result JSON to file")
    parser.add_argument("--coordinator-model", "-m", default="minimax",
                        choices=["minimax", "anthropic"],
                        help="Model for coordinator reasoning (default: minimax)")

    args = parser.parse_args()

    MODEL_MAP = {
        "minimax": "MiniMax-M2.7",
        "anthropic": "anthropic",
    }

    config = AgenticCoderConfig(
        workspace=args.workspace,
        scratch_dir=args.scratch_dir,
        max_workers=args.max_workers,
        coordinator_model=MODEL_MAP.get(args.coordinator_model, args.coordinator_model),
        verbose=not args.quiet,
        timeout_per_worker_minutes=args.timeout,
    )

    coder = AgenticCoder(config)
    result = coder.run(args.goal)

    # Summary
    print(f"\n{'='*60}")
    print(f"Agentic Coder — Done")
    print(f"{'='*60}")
    print(f"Goal: {args.goal}")
    print(f"Workspace: {args.workspace}")
    print(f"Phases: {len(result.phases)}")
    for p in result.phases:
        print(f"  {p.phase:20s}  {p.status}")
    print(f"Final: {result.final_status.upper()}")
    print(f"Scratch: {result.scratch_dir}")
    if result.spec:
        print(f"\n--- Spec Preview ---")
        print(result.spec[:500])
        if len(result.spec) > 500:
            print("... [truncated]")

    if args.output:
        with open(args.output, "w") as f:
            json.dump({
                "goal": args.goal,
                "workspace": args.workspace,
                "scratch_dir": result.scratch_dir,
                "final_status": result.final_status,
                "spec": result.spec,
                "phases": [
                    {"phase": p.phase, "status": p.status, "coordinator_output": p.coordinator_output[:500]}
                    for p in result.phases
                ],
            }, f, indent=2)
        print(f"\nResult written to: {args.output}")

    sys.exit(0 if result.final_status == "ready" else 1)


if __name__ == "__main__":
    main()
