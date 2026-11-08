#!/Users/devgwardo/.hermes/hermes-agent/venv/bin/python
"""
Agentic Coder Terminal — OpenCode-style REPL.

Usage:
    python -m agentic_coder.ui --workspace /path/to/project
    python -m agentic_coder.ui -w /path/to/project --max-workers 3
"""

import sys
from pathlib import Path

# Add parent dir to path so 'ui' subpackage resolves
sys.path.insert(0, str(Path(__file__).parent.parent))

from ui import TerminalUI


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Agentic Coder Terminal — OpenCode clone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m agentic_coder.ui -w ~/my-project
  python -m agentic_coder.ui --workspace ~/my-project --max-workers 5
  python -m agentic_coder.ui -w ~/my-project --scratch-dir /tmp/ac-scratch
        """,
    )
    parser.add_argument(
        "--workspace", "-w",
        required=True,
        help="Project directory",
    )
    parser.add_argument(
        "--scratch-dir", "-s",
        default=None,
        help="Scratch directory (auto-generated if omitted)",
    )
    parser.add_argument(
        "--max-workers", "-n",
        type=int,
        default=3,
        help="Max parallel workers per phase (default: 3)",
    )
    args = parser.parse_args()

    ui = TerminalUI(
        workspace=args.workspace,
        scratch_dir=args.scratch_dir,
        max_workers=args.max_workers,
    )
    ui.run()


if __name__ == "__main__":
    import os
    os.environ.setdefault("PYTHONPATH", str(Path(__file__).parent.parent))
    main()
