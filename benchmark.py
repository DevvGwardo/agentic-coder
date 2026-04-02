#!/usr/bin/env python3
"""
Benchmark harness for Agentic Coder.

Runs a suite of goals against small, disposable workspaces and tracks:
  - Wall-clock time per run
  - Peak RSS (resident set size) via /usr/bin/time
  - Exit code and final status
  - Per-phase breakdown from result JSON

Designed for a 24GB MacBook — uses ulimit, small workspaces, sequential
execution, and configurable worker caps to stay well within memory.

Usage:
    python benchmark.py                     # run all benchmarks
    python benchmark.py --suite smoke       # quick smoke test (1 goal)
    python benchmark.py --suite full        # all goals
    python benchmark.py --max-workers 2     # override worker count
    python benchmark.py --ram-limit-gb 4    # per-run RSS cap (GB)
    python benchmark.py --list              # list available goals
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Test workspaces ─────────────────────────────────────────────────────────
# Each workspace is a tiny, self-contained project that exercises different
# aspects of the agentic coder without requiring large dependencies.

WORKSPACES = {
    "python-minimal": {
        "files": {
            "src/main.py": (
                'def greet(name: str) -> str:\n'
                '    """Return a greeting."""\n'
                '    return f"Hello, {name}!"\n'
                '\n'
                'def add(a: int, b: int) -> int:\n'
                '    return a + b\n'
                '\n'
                'if __name__ == "__main__":\n'
                '    print(greet("world"))\n'
            ),
            "src/utils.py": (
                'import os\n'
                '\n'
                'def read_config(path: str) -> dict:\n'
                '    """Read a JSON config file."""\n'
                '    import json\n'
                '    with open(path) as f:\n'
                '        return json.load(f)\n'
                '\n'
                'def ensure_dir(path: str) -> None:\n'
                '    os.makedirs(path, exist_ok=True)\n'
            ),
            "tests/__init__.py": "",
            "README.md": "# Test Project\nA minimal Python project for benchmarking.\n",
        },
    },
    "python-web": {
        "files": {
            "app.py": (
                'from http.server import HTTPServer, BaseHTTPRequestHandler\n'
                'import json\n'
                '\n'
                'ITEMS = []\n'
                '\n'
                'class Handler(BaseHTTPRequestHandler):\n'
                '    def do_GET(self):\n'
                '        if self.path == "/items":\n'
                '            self.send_response(200)\n'
                '            self.send_header("Content-Type", "application/json")\n'
                '            self.end_headers()\n'
                '            self.wfile.write(json.dumps(ITEMS).encode())\n'
                '        else:\n'
                '            self.send_response(404)\n'
                '            self.end_headers()\n'
                '\n'
                'def run(port=8080):\n'
                '    server = HTTPServer(("", port), Handler)\n'
                '    server.serve_forever()\n'
                '\n'
                'if __name__ == "__main__":\n'
                '    run()\n'
            ),
            "requirements.txt": "# stdlib only\n",
        },
    },
    "js-minimal": {
        "files": {
            "src/index.js": (
                'function fibonacci(n) {\n'
                '  if (n <= 1) return n;\n'
                '  return fibonacci(n - 1) + fibonacci(n - 2);\n'
                '}\n'
                '\n'
                'function isPrime(n) {\n'
                '  if (n < 2) return false;\n'
                '  for (let i = 2; i * i <= n; i++) {\n'
                '    if (n % i === 0) return false;\n'
                '  }\n'
                '  return true;\n'
                '}\n'
                '\n'
                'module.exports = { fibonacci, isPrime };\n'
            ),
            "package.json": json.dumps({
                "name": "bench-test",
                "version": "1.0.0",
                "main": "src/index.js",
            }, indent=2) + "\n",
        },
    },
}


# ─── Benchmark goals ────────────────────────────────────────────────────────

@dataclass
class BenchGoal:
    name: str
    goal: str
    workspace: str  # key into WORKSPACES
    description: str
    suite: list[str] = field(default_factory=lambda: ["full"])


# ─── Real-world workspace: copy of the actual project ───────────────────────
# "self" is a special workspace key — instead of creating files from a dict,
# we clone the agentic_coder directory itself into a temp location so the
# agent works on real code without touching the original.

SELF_WORKSPACE_MARKER = "__self__"

GOALS = [
    # ── Smoke (synthetic) ────────────────────────────────────────────────────
    BenchGoal(
        name="add-tests",
        goal="Add unit tests for all functions in src/main.py using pytest",
        workspace="python-minimal",
        description="Tests basic research + implementation flow",
        suite=["smoke", "full"],
    ),

    # ── Full (synthetic) ─────────────────────────────────────────────────────
    BenchGoal(
        name="code-review",
        goal="Review all Python files and write a code review report to review.md with suggestions for improvement",
        workspace="python-minimal",
        description="Tests research phase depth — read-only analysis",
        suite=["full"],
    ),
    BenchGoal(
        name="add-endpoint",
        goal="Add a POST /items endpoint that accepts JSON {name, price} and appends to the ITEMS list",
        workspace="python-web",
        description="Tests spec→implementation accuracy",
        suite=["full"],
    ),
    BenchGoal(
        name="refactor-classes",
        goal="Refactor src/main.py to use a Calculator class with greet and add as methods",
        workspace="python-minimal",
        description="Tests coordinated multi-file changes",
        suite=["full"],
    ),
    BenchGoal(
        name="add-js-tests",
        goal="Add unit tests for fibonacci and isPrime using plain Node assert (no npm install needed)",
        workspace="js-minimal",
        description="Tests non-Python workspace handling",
        suite=["full"],
    ),
    BenchGoal(
        name="add-validation",
        goal="Add input validation to read_config in src/utils.py: check file exists, handle invalid JSON, return helpful errors",
        workspace="python-minimal",
        description="Tests verification phase — needs to check error paths",
        suite=["full"],
    ),

    # ── Real-world: agent works on its own codebase ──────────────────────────
    BenchGoal(
        name="rw-audit-engine",
        goal=(
            "Read engine.py and write a detailed audit report to audit.md covering: "
            "1) error handling gaps, 2) security concerns in the worker subprocess shell tool, "
            "3) potential race conditions in parallel worker execution, "
            "4) suggestions for improving the MiniMax API retry logic"
        ),
        workspace=SELF_WORKSPACE_MARKER,
        description="Real code: deep audit of engine.py (research-heavy)",
        suite=["realworld"],
    ),
    BenchGoal(
        name="rw-add-retry",
        goal=(
            "Add exponential backoff retry logic to the _complete_minimax function in engine.py. "
            "Retry up to 3 times on connection errors or HTTP 429/500/502/503. "
            "Use a base delay of 1 second with 2x multiplier. Add jitter. "
            "Do not add any new dependencies — use only stdlib."
        ),
        workspace=SELF_WORKSPACE_MARKER,
        description="Real code: add retry logic to engine.py (implementation)",
        suite=["realworld"],
    ),
    BenchGoal(
        name="rw-add-metrics",
        goal=(
            "Add a simple metrics collector to engine.py that tracks per-phase wall time, "
            "number of workers spawned, number of API calls, and total tokens used. "
            "Store metrics in a dataclass and include them in CoderResult. "
            "Print a metrics summary table at the end of each run."
        ),
        workspace=SELF_WORKSPACE_MARKER,
        description="Real code: add metrics to engine.py (multi-file change)",
        suite=["realworld"],
    ),
    BenchGoal(
        name="rw-harden-worker",
        goal=(
            "Harden the worker subprocess in engine.py: "
            "1) Add a file size limit (max 1MB) to tool_read_file, "
            "2) Add path traversal protection to tool_write_file — block writes outside WORKSPACE and SCRATCH_DIR, "
            "3) Add a command blocklist to tool_shell — block rm -rf /, curl to external IPs, etc. "
            "Write the changes directly in engine.py."
        ),
        workspace=SELF_WORKSPACE_MARKER,
        description="Real code: security hardening (verification-heavy)",
        suite=["realworld"],
    ),
    BenchGoal(
        name="rw-dry-run-mode",
        goal=(
            "Add a --dry-run flag to cli.py that runs the Research and Synthesis phases "
            "but skips Implementation and Verification. It should print the generated spec "
            "and exit with code 0. Update AgenticCoderConfig to support a dry_run boolean "
            "and modify the run() method in engine.py to respect it."
        ),
        workspace=SELF_WORKSPACE_MARKER,
        description="Real code: add CLI flag across cli.py + engine.py",
        suite=["realworld"],
    ),
]


# ─── Result types ────────────────────────────────────────────────────────────

@dataclass
class BenchResult:
    name: str
    goal: str
    workspace: str
    elapsed_s: float
    peak_rss_mb: float
    exit_code: int
    final_status: str
    phases: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    stdout_tail: str = ""
    api_calls: int = 0
    total_tokens: int = 0


# ─── Workspace setup / teardown ──────────────────────────────────────────────

def create_workspace(spec_or_marker, base_dir: str, project_dir: str) -> str:
    """Create a temp workspace. Returns the path.

    If spec_or_marker is SELF_WORKSPACE_MARKER, copies the real project.
    Otherwise creates files from the spec dict.
    """
    if spec_or_marker == SELF_WORKSPACE_MARKER:
        ws_dir = tempfile.mkdtemp(dir=base_dir, prefix="bench-self-")
        # Copy only .py files and key config — skip .git, __pycache__, scratch dirs
        for item in os.listdir(project_dir):
            src = os.path.join(project_dir, item)
            dst = os.path.join(ws_dir, item)
            if item.startswith(".") or item == "__pycache__" or item.startswith("bench-"):
                continue
            if os.path.isfile(src):
                shutil.copy2(src, dst)
            elif os.path.isdir(src):
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".git", "node_modules",
                ))
        return ws_dir

    ws_dir = tempfile.mkdtemp(dir=base_dir, prefix="bench-ws-")
    for rel_path, content in spec_or_marker["files"].items():
        full = os.path.join(ws_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    return ws_dir


# ─── Memory tracking ────────────────────────────────────────────────────────

def parse_macos_time_output(stderr: str) -> float:
    """Extract peak RSS in MB from /usr/bin/time -l output on macOS."""
    # macOS format: "  12345678  maximum resident set size" (bytes)
    m = re.search(r"(\d+)\s+maximum resident set size", stderr)
    if m:
        return int(m.group(1)) / (1024 * 1024)  # bytes → MB
    return 0.0


def parse_linux_time_output(stderr: str) -> float:
    """Extract peak RSS in MB from /usr/bin/time -v output on Linux."""
    m = re.search(r"Maximum resident set size.*?:\s*(\d+)", stderr)
    if m:
        return int(m.group(1)) / 1024  # KB → MB
    return 0.0


# ─── Workspace diffing ───────────────────────────────────────────────────────

def snapshot_workspace(ws_dir: str) -> dict[str, str]:
    """Read all text files in workspace into a dict {relative_path: content}."""
    snap = {}
    for root, _dirs, files in os.walk(ws_dir):
        for fname in files:
            if fname.endswith((".pyc", ".log")) or fname.startswith("."):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, ws_dir)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    snap[rel] = f.read()
            except Exception:
                pass
    return snap


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> str:
    """Produce a unified diff between two workspace snapshots."""
    import difflib
    lines = []
    all_files = sorted(set(before) | set(after))
    for f in all_files:
        old = before.get(f, "")
        new = after.get(f, "")
        if old == new:
            continue
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{f}",
            tofile=f"b/{f}",
            lineterm="",
        )
        lines.extend(diff)
    if not lines:
        return "(no changes)"
    return "\n".join(lines)


# ─── Run a single benchmark ─────────────────────────────────────────────────

def run_benchmark(
    bench: BenchGoal,
    base_dir: str,
    cli_path: str,
    max_workers: int,
    timeout_minutes: int,
    ram_limit_gb: float,
    show_diff: bool = False,
) -> BenchResult:
    """Run a single benchmark goal and return the result."""
    # Create fresh workspace
    project_dir = os.path.dirname(os.path.abspath(__file__))
    if bench.workspace == SELF_WORKSPACE_MARKER:
        ws_dir = create_workspace(SELF_WORKSPACE_MARKER, base_dir, project_dir)
    else:
        ws_dir = create_workspace(WORKSPACES[bench.workspace], base_dir, project_dir)
    result_json = os.path.join(base_dir, f"result-{bench.name}.json")

    # Build command with /usr/bin/time for memory tracking
    is_macos = sys.platform == "darwin"
    time_flag = "-l" if is_macos else "-v"

    cmd = [
        "/usr/bin/time", time_flag,
        sys.executable, cli_path,
        bench.goal,
        "-w", ws_dir,
        "-n", str(max_workers),
        "-t", str(timeout_minutes),
        "-o", result_json,
        "-q",  # quiet mode — less noise
    ]

    # Set memory limit via environment (soft limit, in bytes)
    env = os.environ.copy()
    ram_limit_bytes = int(ram_limit_gb * 1024 * 1024 * 1024)

    # Snapshot workspace before run (for --diff)
    before_snap = snapshot_workspace(ws_dir) if show_diff else {}

    print(f"  [{bench.name}] Starting... (workers={max_workers}, ram_cap={ram_limit_gb}GB)")
    start = time.time()

    try:
        # Use ulimit via shell wrapper for memory cap
        shell_cmd = f"ulimit -v {int(ram_limit_gb * 1024 * 1024)} 2>/dev/null; " + " ".join(
            shlex.quote(c) for c in cmd
        )
        proc = subprocess.run(
            shell_cmd,
            shell=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_minutes * 60 * 4 + 60,  # 4 phases + grace period
        )
        elapsed = time.time() - start

        # Parse memory from /usr/bin/time output (goes to stderr)
        if is_macos:
            peak_rss = parse_macos_time_output(proc.stderr)
        else:
            peak_rss = parse_linux_time_output(proc.stderr)

        # Read result JSON if it was written
        phases = []
        final_status = "unknown"
        scratch_dir = ""
        if os.path.exists(result_json):
            try:
                with open(result_json) as f:
                    data = json.load(f)
                phases = data.get("phases", [])
                final_status = data.get("final_status", "unknown")
                scratch_dir = data.get("scratch_dir", "")
            except Exception:
                pass

        # Infer status from exit code if JSON wasn't written
        if final_status == "unknown":
            final_status = "ready" if proc.returncode == 0 else "failed"

        # Diff workspace if requested
        if show_diff and before_snap:
            after_snap = snapshot_workspace(ws_dir)
            diff_text = diff_snapshots(before_snap, after_snap)
            if diff_text != "(no changes)":
                print(f"  [{bench.name}] Workspace diff:")
                # Limit diff output to avoid flooding terminal
                diff_lines = diff_text.split("\n")
                for line in diff_lines[:50]:
                    print(f"    {line}")
                if len(diff_lines) > 50:
                    print(f"    ... ({len(diff_lines) - 50} more lines)")

        # Read API metrics from scratch dir
        api_calls = 0
        total_tokens = 0
        if scratch_dir:
            metrics_path = os.path.join(scratch_dir, "metrics.json")
            if os.path.exists(metrics_path):
                try:
                    with open(metrics_path) as f:
                        metrics = json.load(f)
                    totals = metrics.get("totals", {})
                    api_calls = totals.get("api_calls", 0)
                    total_tokens = totals.get("total_tokens", 0)
                except Exception:
                    pass

        return BenchResult(
            name=bench.name,
            goal=bench.goal,
            workspace=bench.workspace,
            elapsed_s=round(elapsed, 1),
            peak_rss_mb=round(peak_rss, 1),
            exit_code=proc.returncode,
            final_status=final_status,
            phases=phases,
            stdout_tail=proc.stdout[-300:] if proc.stdout else "",
            api_calls=api_calls,
            total_tokens=total_tokens,
        )

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return BenchResult(
            name=bench.name,
            goal=bench.goal,
            workspace=bench.workspace,
            elapsed_s=round(elapsed, 1),
            peak_rss_mb=0,
            exit_code=-1,
            final_status="timeout",
            error=f"Exceeded {timeout_minutes}min timeout",
        )
    except Exception as e:
        elapsed = time.time() - start
        return BenchResult(
            name=bench.name,
            goal=bench.goal,
            workspace=bench.workspace,
            elapsed_s=round(elapsed, 1),
            peak_rss_mb=0,
            exit_code=-1,
            final_status="error",
            error=str(e),
        )
    finally:
        # Clean up workspace
        shutil.rmtree(ws_dir, ignore_errors=True)


# ─── Report ──────────────────────────────────────────────────────────────────

def print_report(results: list[BenchResult], output_file: Optional[str] = None):
    """Print a summary table and optionally write full results to JSON."""
    print()
    print("=" * 80)
    print("  BENCHMARK RESULTS")
    print("=" * 80)
    print(f"  {'Name':<20s} {'Status':<8s} {'Time':>8s} {'RSS MB':>7s} {'API':>5s} {'Tokens':>8s} {'Phases':>7s}")
    print("-" * 80)

    total_time = 0
    peak_rss = 0
    total_api = 0
    total_tok = 0
    passed = 0

    for r in results:
        phase_str = str(len(r.phases)) if r.phases else "-"
        status_icon = "PASS" if r.final_status == "ready" else "FAIL"
        api_str = str(r.api_calls) if r.api_calls else "-"
        tok_str = f"{r.total_tokens:,}" if r.total_tokens else "-"
        print(f"  {r.name:<20s} {status_icon:<8s} {r.elapsed_s:>7.1f}s {r.peak_rss_mb:>6.1f} {api_str:>5s} {tok_str:>8s} {phase_str:>7s}")
        total_time += r.elapsed_s
        peak_rss = max(peak_rss, r.peak_rss_mb)
        total_api += r.api_calls
        total_tok += r.total_tokens
        if r.final_status == "ready":
            passed += 1

    print("-" * 80)
    print(f"  Total: {len(results)} goals, {passed} passed, {len(results) - passed} failed")
    print(f"  Wall time: {total_time:.1f}s ({total_time/60:.1f}min)")
    print(f"  Peak RSS across all runs: {peak_rss:.1f} MB")
    if total_api:
        print(f"  Total API calls: {total_api}, tokens: {total_tok:,}")
    print("=" * 80)

    # Errors detail
    errors = [r for r in results if r.error]
    if errors:
        print("\n  ERRORS:")
        for r in errors:
            print(f"    [{r.name}] {r.error}")

    if output_file:
        with open(output_file, "w") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "summary": {
                    "total": len(results),
                    "passed": passed,
                    "failed": len(results) - passed,
                    "total_time_s": round(total_time, 1),
                    "peak_rss_mb": round(peak_rss, 1),
                },
                "results": [asdict(r) for r in results],
            }, f, indent=2)
        print(f"\n  Full results written to: {output_file}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Benchmark harness for Agentic Coder (RAM-friendly)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--suite", choices=["smoke", "full", "realworld"], default="smoke",
                        help="Goal suite to run (default: smoke)")
    parser.add_argument("--max-workers", type=int, default=1,
                        help="Max parallel workers per run (default: 1 — safest for RAM)")
    parser.add_argument("--timeout", type=int, default=5,
                        help="Timeout per worker in minutes (default: 5). Total run timeout = 4x this (for 4 phases)")
    parser.add_argument("--ram-limit-gb", type=float, default=4.0,
                        help="Per-run RSS limit in GB (default: 4)")
    parser.add_argument("--output", "-o", default=None,
                        help="Write full results JSON to file")
    parser.add_argument("--list", action="store_true",
                        help="List available benchmark goals and exit")
    parser.add_argument("--goal", type=str, default=None,
                        help="Run a single goal by name")
    parser.add_argument("--diff", action="store_true",
                        help="Show workspace file diffs after each run")
    args = parser.parse_args()

    if args.list:
        print("Available benchmark goals:")
        for g in GOALS:
            suites = ", ".join(g.suite)
            print(f"  {g.name:<20s} [{suites}]  {g.description}")
        return

    # Select goals
    if args.goal:
        selected = [g for g in GOALS if g.name == args.goal]
        if not selected:
            print(f"Unknown goal: {args.goal}")
            print("Use --list to see available goals")
            sys.exit(1)
    else:
        selected = [g for g in GOALS if args.suite in g.suite]

    cli_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")
    if not os.path.exists(cli_path):
        print(f"ERROR: cli.py not found at {cli_path}")
        sys.exit(1)

    print(f"Agentic Coder Benchmark")
    print(f"  Suite: {args.suite} ({len(selected)} goals)")
    print(f"  Workers: {args.max_workers}")
    print(f"  Timeout: {args.timeout}min/worker")
    print(f"  RAM limit: {args.ram_limit_gb}GB/run")
    print()

    # Create a shared temp base for all workspaces
    base_dir = tempfile.mkdtemp(prefix="agentic-bench-")

    results = []
    try:
        for i, bench in enumerate(selected, 1):
            print(f"[{i}/{len(selected)}] {bench.name}: {bench.description}")
            result = run_benchmark(
                bench=bench,
                base_dir=base_dir,
                cli_path=cli_path,
                max_workers=args.max_workers,
                timeout_minutes=args.timeout,
                ram_limit_gb=args.ram_limit_gb,
                show_diff=args.diff,
            )
            results.append(result)

            # Print quick inline status
            rss_str = f"{result.peak_rss_mb:.0f}MB" if result.peak_rss_mb else "N/A"
            print(f"  → {result.final_status} in {result.elapsed_s:.1f}s (peak {rss_str})")
            print()

    except KeyboardInterrupt:
        print("\n  Interrupted — showing partial results\n")

    finally:
        print_report(results, args.output)
        # Clean up temp dir
        shutil.rmtree(base_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
