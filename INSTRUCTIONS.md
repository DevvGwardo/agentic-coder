# Agentic Coder — Hermes Integration Guide

Complete instructions for deploying and operating Agentic Coder as part of the Hermes Agent ecosystem.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Environment Setup](#environment-setup)
5. [Running from Hermes](#running-from-hermes)
   - [Direct Python Import](#direct-python-import)
   - [CLI Invocation](#cli-invocation)
   - [Heartbeat / Cron Integration](#heartbeat--cron-integration)
   - [Hermes Skill Integration](#hermes-skill-integration)
6. [Configuration Reference](#configuration-reference)
7. [Understanding the Four Phases](#understanding-the-four-phases)
8. [Reading the Output](#reading-the-output)
9. [Worker Architecture](#worker-architecture)
10. [MiniMax Proxy Setup](#minimax-proxy-setup)
11. [Hermes-Evo Integration](#hermes-evo-integration)
12. [Operational Playbook](#operational-playbook)
13. [Troubleshooting](#troubleshooting)
14. [Advanced Usage](#advanced-usage)

---

## Overview

Agentic Coder is a four-phase autonomous coding agent that lives inside the Hermes-Evo ecosystem. It takes a natural language goal (e.g. "Add rate limiting to the proxy endpoints") and autonomously:

1. **Researches** the target codebase with parallel workers
2. **Synthesizes** findings into an actionable spec
3. **Implements** changes with parallel workers
4. **Verifies** the result against the spec

All LLM calls go through MiniMax-M2.7. The coordinator reasons about the task; workers are detached Python subprocesses that call the MiniMax API directly and execute tool calls (file read/write, shell commands, directory listing).

Hermes can invoke Agentic Coder in several ways:
- **Directly** from a Hermes session via Python import
- **Via CLI** from a terminal or shell tool call
- **On a schedule** via Hermes cron jobs
- **As a skill** deployed to `~/.hermes/skills/`

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.9+ (stdlib only, no pip dependencies) |
| MiniMax API access | Either a direct API key or a local proxy (e.g. codex-minimax-proxy) |
| Hermes Agent | Running instance with tool access (terminal, read_file, write_file) |
| Disk space | ~50MB per run (scratch dir artifacts) |

---

## Installation

Agentic Coder ships as part of `hermes-evo`. No separate install is needed.

```
~/hermes-evo/
  agentic_coder/
    __init__.py      # Package exports
    engine.py        # Core engine — coordinator + worker spawning
    cli.py           # Standalone CLI entry point
    SKILL.md         # Skill manifest for Hermes deployment
```

If you cloned the standalone repo:

```bash
git clone https://github.com/DevvGwardo/agentic-coder.git ~/hermes-evo/agentic_coder
```

Verify the install:

```bash
python3 -c "import sys; sys.path.insert(0, '$HOME/hermes-evo/agentic_coder'); from engine import AgenticCoder; print('OK')"
```

---

## Environment Setup

Set these environment variables before running. Add them to your shell profile (`~/.zshrc`) or Hermes config.

### Required

```bash
export MINIMAX_API_KEY="your-minimax-api-key"
```

### Optional

```bash
# MiniMax API endpoint (default: http://localhost:4000)
# Point this to your codex-minimax-proxy or directly to MiniMax
export MINIMAX_API_BASE="http://localhost:4000"

# Parent directory for auto-generated scratch dirs (default: /tmp)
# Each run creates a timestamped subdirectory here
export HERMES_AGENTIC_CODER_DIR="/tmp"
```

### Verifying API Access

```bash
curl -s "$MINIMAX_API_BASE/v1/chat/completions" \
  -H "Authorization: Bearer $MINIMAX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"MiniMax-M2.7","messages":[{"role":"user","content":"ping"}],"max_tokens":10}' \
  | python3 -m json.tool
```

You should see a valid chat completion response. If you get connection refused, start your proxy first.

---

## Running from Hermes

### Direct Python Import

The most common way to use Agentic Coder from within a Hermes session or script:

```python
import sys
sys.path.insert(0, "/Users/devgwardo/hermes-evo/agentic_coder")

from engine import AgenticCoder, AgenticCoderConfig

config = AgenticCoderConfig(
    workspace="/path/to/target/project",  # The codebase to work on
    max_workers=3,                         # Parallel workers per phase
    verbose=True,                          # Timestamped logs to stdout
    timeout_per_worker_minutes=10,         # Kill workers after this
)

coder = AgenticCoder(config)
result = coder.run("Add input validation to all API endpoints")

# Check result
if result.final_status == "ready":
    print("Changes implemented and verified!")
    print(f"Spec: {result.spec[:500]}")
else:
    print("Needs more work. Check scratch dir for details.")
    print(f"Scratch: {result.scratch_dir}")

# Inspect individual phases
for phase in result.phases:
    print(f"  {phase.phase}: {phase.status}")
    if phase.error:
        print(f"    Error: {phase.error}")
```

### CLI Invocation

Run from terminal, or from Hermes via the `terminal` tool:

```bash
python3 ~/hermes-evo/agentic_coder/cli.py \
  "Refactor the database layer to use connection pooling" \
  --workspace ~/my-project \
  --max-workers 3 \
  --timeout 15 \
  --output /tmp/coder-result.json
```

#### CLI Arguments

| Argument | Short | Required | Default | Description |
|---|---|---|---|---|
| `goal` | (positional) | Yes | -- | Natural language coding task |
| `--workspace` | `-w` | Yes | -- | Target codebase directory |
| `--scratch-dir` | `-s` | No | auto | Working directory for artifacts |
| `--max-workers` | `-n` | No | 3 | Max parallel workers per phase |
| `--timeout` | `-t` | No | 10 | Timeout per worker (minutes) |
| `--output` | `-o` | No | -- | Write result JSON to file |
| `--verbose` | `-v` | No | True | Verbose output |
| `--quiet` | `-q` | No | False | Suppress logs |
| `--coordinator-model` | `-m` | No | minimax | Model for coordinator (minimax or anthropic) |

#### Exit Codes

| Code | Meaning |
|---|---|
| 0 | `final_status == "ready"` — all phases passed |
| 1 | `final_status == "needs_work"` — verification failed or phase blocked |

### Heartbeat / Cron Integration

Hermes supports scheduled tasks via cron jobs. Use this to run Agentic Coder on a schedule — for example, to periodically improve a project.

#### Setting Up a Hermes Cron Job

```bash
mcp_cronjob create \
  --prompt "python3 ~/hermes-evo/agentic_coder/cli.py 'Review and fix any TODO comments in the codebase' --workspace ~/my-project --output /tmp/agentic-coder-latest.json" \
  --schedule "0 3 * * *" \
  --name "agentic-coder-nightly"
```

This runs Agentic Coder every night at 3 AM.

#### Heartbeat Pattern (from Hermes session)

If Hermes has a heartbeat loop, you can trigger Agentic Coder from it:

```python
import sys, json, os
sys.path.insert(0, os.path.expanduser("~/hermes-evo/agentic_coder"))
from engine import AgenticCoder, AgenticCoderConfig

def heartbeat_coding_task(goal: str, workspace: str):
    """Called from Hermes heartbeat when a coding task is queued."""
    config = AgenticCoderConfig(
        workspace=workspace,
        scratch_dir=f"/tmp/agentic-coder-heartbeat-{int(__import__('time').time())}",
        max_workers=3,
        timeout_per_worker_minutes=10,
    )
    coder = AgenticCoder(config)
    result = coder.run(goal)

    # Log result for Hermes to pick up
    log_entry = {
        "goal": goal,
        "workspace": workspace,
        "status": result.final_status,
        "scratch_dir": result.scratch_dir,
        "phases": [{"phase": p.phase, "status": p.status} for p in result.phases],
    }
    with open(os.path.expanduser("~/hermes-evo/agentic_coder_runs.jsonl"), "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    return result
```

### Hermes Skill Integration

Agentic Coder can be deployed as a Hermes skill so it's available from any session.

#### Deploy as a Skill

Copy the skill to the Hermes skills directory:

```bash
mkdir -p ~/.hermes/skills/agentic-coder
cp ~/hermes-evo/agentic_coder/SKILL.md ~/.hermes/skills/agentic-coder/SKILL.md
cp ~/hermes-evo/agentic_coder/engine.py ~/.hermes/skills/agentic-coder/engine.py
cp ~/hermes-evo/agentic_coder/cli.py ~/.hermes/skills/agentic-coder/cli.py
cp ~/hermes-evo/agentic_coder/__init__.py ~/.hermes/skills/agentic-coder/__init__.py
```

Create the skill metadata:

```bash
cat > ~/.hermes/skills/agentic-coder/skill.json << 'EOF'
{
  "id": "agentic-coder",
  "name": "Agentic Coder",
  "version": "1.1.0",
  "description": "Four-phase autonomous coding agent — MiniMax coordinator + worker architecture",
  "trigger_phrases": [
    "agentic coder",
    "autonomous coding",
    "multi-agent coding",
    "parallel coding agent",
    "run agentic coder"
  ],
  "entry": "cli.py"
}
EOF
```

#### Or Deploy via Hermes-Evo Pipeline

If Hermes-Evo is running on a cron schedule, it will detect, validate, and deploy skills automatically:

```bash
# Run the full Hermes-Evo pipeline (detector -> generator -> validator -> deployer)
cd ~/hermes-evo/scripts && \
  python3 detector.py && \
  python3 generator.py && \
  python3 validator.py && \
  python3 deployer.py
```

Check deployment status:

```bash
python3 ~/hermes-evo/scripts/improvement_log.py summary
```

---

## Configuration Reference

### AgenticCoderConfig Fields

```python
@dataclass
class AgenticCoderConfig:
    workspace: str                        # REQUIRED — target project directory
    scratch_dir: str | None = None        # Auto-generated if omitted
    max_workers: int = 3                  # Workers per phase (research + implementation)
    coordinator_model: str = "MiniMax-M2.7"  # LLM for planning/synthesis
    worker_model: str = "MiniMax-M2.7"       # LLM for workers (same for now)
    verbose: bool = True                  # Print timestamped logs
    timeout_per_worker_minutes: int = 10  # Kill stalled workers
```

### Tuning Recommendations

| Scenario | `max_workers` | `timeout` | Notes |
|---|---|---|---|
| Small fix (1-3 files) | 2 | 5 min | Fast, focused |
| Medium feature (5-10 files) | 3 | 10 min | Default — good balance |
| Large refactor (10+ files) | 4-5 | 15 min | More workers = more parallel coverage |
| Exploration / audit | 3 | 10 min | Research phase is most valuable here |

### Scratch Directory Auto-Naming

When `scratch_dir` is not provided, it's auto-generated as:

```
$HERMES_AGENTIC_CODER_DIR/agentic-coder-YYYYMMDD_HHMMSS/
```

Default parent is `/tmp`. Override with the `HERMES_AGENTIC_CODER_DIR` env var to persist artifacts.

---

## Understanding the Four Phases

### Phase 1: Research

**What happens:**
- The coordinator (MiniMax) receives the goal and workspace path
- It creates a research plan splitting the goal into N independent investigation tasks
- N worker subprocesses are spawned in parallel
- Each worker explores the codebase using tools: `read_file`, `list_dir`, `shell`, `path_exists`
- Workers write findings to `worker-{id}-research.md`
- The coordinator waits for all workers (polls every 5 seconds, force-kills at timeout)

**Artifacts:**
- `plan.md` — coordinator's research plan with worker assignments
- `worker-r1-research.md`, `worker-r2-research.md`, etc. — individual findings
- `worker-r1.log`, etc. — raw subprocess stdout

**When it fails:**
- Workers hit iteration cap (30) without writing reports
- API timeout or rate limiting
- Workspace path doesn't exist

### Phase 2: Synthesis

**What happens:**
- The coordinator pre-reads all `worker-*-research.md` files from the scratch dir
- These findings are injected directly into the synthesis prompt
- The coordinator distills findings into a concrete, actionable specification
- The engine extracts the spec from the coordinator's response and writes `spec.md`

**Artifacts:**
- `spec.md` — the implementation specification (exact files, exact changes, constraints)

**When it fails:**
- No research reports found (Phase 1 failed)
- Coordinator returns empty or unparseable response
- Spec extraction fails (no code blocks or recognizable structure)

### Phase 3: Implementation

**What happens:**
- The coordinator pre-reads `spec.md` (first 2000 chars)
- It creates an implementation plan splitting the spec into N worker tasks
- N worker subprocesses are spawned in parallel
- Each worker makes targeted code changes using `write_file` and `shell`
- Workers write reports to `worker-{id}-implementation.md`

**Artifacts:**
- `impl-plan.md` — coordinator's implementation plan with worker assignments
- `worker-i1-implementation.md`, etc. — implementation reports
- `worker-i1.log`, etc. — raw subprocess stdout

**When it fails:**
- Workers modify wrong files or make incomplete changes
- Workers hit iteration cap before writing reports
- Spec is too vague for workers to act on

### Phase 4: Verification

**What happens:**
- The coordinator reads the spec and all implementation reports
- It verifies each spec item was implemented correctly
- Reports pass/fail per item
- Final status: `"ready"` or `"needs_work"`

**Artifacts:**
- `verification.md` — pass/fail checklist against spec

**When it fails:**
- Implementation reports missing
- Coordinator marks items as failed

---

## Reading the Output

### CoderResult Object

```python
result = coder.run("...")

result.final_status    # "ready" or "needs_work"
result.scratch_dir     # Path to all artifacts
result.spec            # The spec text (from synthesis phase)
result.phases          # List[PhaseResult] — one per phase
```

### PhaseResult Object

```python
phase = result.phases[0]

phase.phase              # "research", "synthesis", "implementation", "verification"
phase.status             # "ok", "error", "blocked"
phase.coordinator_output # Raw coordinator response text
phase.worker_ids         # ["r1", "r2", "r3"] — IDs of spawned workers
phase.error              # Error message if status != "ok"
```

### Scratch Directory Layout

```
/tmp/agentic-coder-20260401_120000/
  plan.md                          # Phase 1: research plan
  worker-r1-research.md            # Phase 1: worker r1 findings
  worker-r2-research.md            # Phase 1: worker r2 findings
  worker-r3-research.md            # Phase 1: worker r3 findings
  worker-r1.log                    # Phase 1: worker r1 subprocess log
  worker-r2.log                    # Phase 1: worker r2 subprocess log
  worker-r3.log                    # Phase 1: worker r3 subprocess log
  spec.md                          # Phase 2: implementation spec
  impl-plan.md                     # Phase 3: implementation plan
  worker-i1-implementation.md      # Phase 3: worker i1 report
  worker-i2-implementation.md      # Phase 3: worker i2 report
  worker-i3-implementation.md      # Phase 3: worker i3 report
  worker-i1.log                    # Phase 3: worker i1 subprocess log
  worker-i2.log                    # Phase 3: worker i2 subprocess log
  worker-i3.log                    # Phase 3: worker i3 subprocess log
  verification.md                  # Phase 4: verification report
```

### Inspecting Results from Terminal

```bash
# Quick status check
cat /tmp/agentic-coder-*/verification.md

# See what the coordinator planned
cat /tmp/agentic-coder-*/plan.md

# Check what workers found
cat /tmp/agentic-coder-*/worker-*-research.md

# See what was implemented
cat /tmp/agentic-coder-*/worker-*-implementation.md

# Debug a stuck worker
tail -100 /tmp/agentic-coder-*/worker-r1.log
```

---

## Worker Architecture

### How Workers Run

Each worker is a **detached Python subprocess** (`subprocess.Popen` with `start_new_session=True`). The entire agent loop is injected as a Python script via `python -c`.

```
Coordinator                           Workers (parallel subprocesses)
    |                                     |
    |-- builds plan.md -------> parse --> worker r1 (pid 12345)
    |                              |----> worker r2 (pid 12346)
    |                              |----> worker r3 (pid 12347)
    |                                     |
    |                                     | (each runs MiniMax agent loop)
    |                                     | (tools: read_file, write_file,
    |                                     |  list_dir, shell, path_exists)
    |                                     |
    |<--- polls logs every 5s ------------|
    |<--- WORKER_DONE marker found -------|
    |                                     |
    |-- reads worker reports              |
    |-- proceeds to next phase            v
```

### Worker Tools

Workers have five tools available via MiniMax function calling:

| Tool | Arguments | Returns | Notes |
|---|---|---|---|
| `read_file` | `path: string` | `{size, content}` | Content truncated to 500 chars (preview) |
| `write_file` | `path: string, content: string` | `{written, path}` | Creates parent dirs automatically |
| `list_dir` | `path: string` | `{entries[], total}` | Capped at 50 entries |
| `shell` | `command: string, timeout?: int` | `{stdout, stderr, rc}` | Runs in workspace dir, stdout capped at 2000 chars |
| `path_exists` | `path: string` | `{path, exists, kind}` | kind: "file", "dir", "missing" |

### Worker Lifecycle

1. Subprocess starts with injected Python script
2. System prompt includes workspace, goal, instruction, and scratch dir
3. Agent loop runs up to `MAX_ITERATIONS=30`
4. Each iteration: call MiniMax API -> execute tool calls -> append results
5. When done, worker writes report to `worker-{id}-{phase}.md` with `<<<WORKER_DONE>>>` marker
6. If iteration cap hit without writing report, a fallback report is created
7. Coordinator polls `worker-{id}.log` for the completion marker every 5 seconds
8. Workers are force-killed after `timeout_per_worker_minutes`

### Worker Completion Detection

The coordinator detects worker completion by:
1. Checking if the process PID is still alive (`ps -p <pid>`)
2. Scanning the worker's log file for `<<<WORKER_DONE>>>`
3. If neither: worker is considered still running

---

## MiniMax Proxy Setup

Agentic Coder expects a MiniMax-compatible API at `$MINIMAX_API_BASE` (default `http://localhost:4000`).

### Option A: Direct MiniMax API

```bash
export MINIMAX_API_BASE="https://api.minimax.chat"
export MINIMAX_API_KEY="your-key-here"
```

### Option B: Local Proxy (Recommended)

If you're running `codex-minimax-proxy` or a similar proxy:

```bash
# Start the proxy
cd ~/codex-minimax-proxy && npm start
# or: python3 proxy.py

# Point Agentic Coder to it
export MINIMAX_API_BASE="http://localhost:4000"
export MINIMAX_API_KEY="your-key"
```

The proxy approach is recommended because:
- Rate limiting and retry logic handled by the proxy
- Request/response logging for debugging
- API key rotation if needed
- Caching for repeated identical queries

### Required API Endpoint

Agentic Coder calls a single endpoint:

```
POST $MINIMAX_API_BASE/v1/chat/completions
Authorization: Bearer $MINIMAX_API_KEY
Content-Type: application/json

{
  "model": "MiniMax-M2.7",
  "messages": [...],
  "max_tokens": 8192,
  "temperature": 0.3,
  "tools": [...]           // workers only
  "tool_choice": "auto"    // workers only
}
```

---

## Hermes-Evo Integration

Agentic Coder is part of the Hermes-Evo self-improvement pipeline. Here's how they connect:

```
Hermes Sessions
      |
      v
hermes-evo/scripts/detector.py     <-- Scans sessions for error patterns
      |
      v
hermes-evo/scripts/generator.py    <-- Generates skills to fix patterns
      |
      v
hermes-evo/scripts/validator.py    <-- Validates generated skills
      |
      v
hermes-evo/scripts/deployer.py     <-- Deploys to ~/.hermes/skills/
      |
      v
hermes-evo/agentic_coder/          <-- Autonomous coding for complex fixes
```

### Using Agentic Coder for Evo-Generated Tasks

When Hermes-Evo detects a pattern that requires code changes (not just a new skill), you can feed it to Agentic Coder:

```python
import json, sys, os
sys.path.insert(0, os.path.expanduser("~/hermes-evo/agentic_coder"))
from engine import AgenticCoder, AgenticCoderConfig

# Read failure report
with open(os.path.expanduser("~/hermes-evo/failure_report.json")) as f:
    report = json.load(f)

# For each high-severity pattern, generate a coding task
for pattern in report["patterns"]:
    if pattern["severity"] in ("CRITICAL", "HIGH"):
        goal = (
            f"Fix the {pattern['errorType']} error in {pattern['toolName']}. "
            f"Pattern: {pattern['messagePrefix']}. "
            f"Seen {pattern['occurrenceCount']} times across {pattern['sessionCount']} sessions."
        )
        config = AgenticCoderConfig(
            workspace=os.path.expanduser("~/.hermes/hermes-agent"),
            max_workers=3,
        )
        result = AgenticCoder(config).run(goal)
        print(f"[{pattern['id']}] {result.final_status}")
```

### Combined Cron Schedule

Run both Hermes-Evo and Agentic Coder on a schedule:

```bash
mcp_cronjob create \
  --prompt "cd ~/hermes-evo/scripts && python3 detector.py && python3 generator.py && python3 validator.py && python3 deployer.py && python3 improvement_log.py summary" \
  --schedule "*/30 * * * *" \
  --name "hermes-evo-pipeline"
```

---

## Operational Playbook

### Running a One-Off Task

```bash
# Simple bug fix
python3 ~/hermes-evo/agentic_coder/cli.py \
  "Fix the TypeError in src/auth.py line 42 where user_id can be None" \
  -w ~/my-project -n 2 -t 5

# Feature implementation
python3 ~/hermes-evo/agentic_coder/cli.py \
  "Add a /health endpoint that returns server uptime and memory usage" \
  -w ~/my-api -n 3 -t 10 -o /tmp/health-endpoint-result.json

# Codebase audit
python3 ~/hermes-evo/agentic_coder/cli.py \
  "Find all SQL injection vulnerabilities and fix them" \
  -w ~/my-webapp -n 4 -t 15
```

### Reviewing Results Before Committing

Always review the changes before committing:

```bash
# 1. Run the coder
python3 ~/hermes-evo/agentic_coder/cli.py \
  "Refactor error handling to use custom exception classes" \
  -w ~/my-project -o /tmp/result.json

# 2. Check what changed
cd ~/my-project && git diff

# 3. Read the spec to understand intent
cat /tmp/agentic-coder-*/spec.md

# 4. Read the verification report
cat /tmp/agentic-coder-*/verification.md

# 5. If satisfied, commit
cd ~/my-project && git add -A && git commit -m "Refactor error handling (via agentic-coder)"
```

### Chaining Multiple Tasks

```python
tasks = [
    "Add type hints to all public functions in src/api/",
    "Write unit tests for src/api/auth.py",
    "Add docstrings to all classes in src/models/",
]

for task in tasks:
    config = AgenticCoderConfig(workspace="/path/to/project", max_workers=3)
    result = AgenticCoder(config).run(task)
    print(f"[{result.final_status}] {task}")
    if result.final_status != "ready":
        print(f"  Check: {result.scratch_dir}")
        break  # Stop on failure
```

### Monitoring Live Runs

While Agentic Coder is running, you can watch progress:

```bash
# Watch coordinator logs
tail -f /tmp/agentic-coder-*/worker-*.log

# Check which workers are still alive
ps aux | grep agentic-coder

# See what artifacts have been created so far
ls -la /tmp/agentic-coder-*/
```

---

## Troubleshooting

### Workers produce empty reports

**Cause:** MiniMax returns content in `reasoning_details` instead of `content`, or `max_tokens` is too low.

**Fix:** This is handled in the engine. If it still happens, check your proxy is passing through the full response including `reasoning_details`.

```bash
# Check a worker log for clues
cat /tmp/agentic-coder-*/worker-r1.log | grep -i "error\|empty\|api"
```

### Workers loop indefinitely

**Cause:** `max_tokens=4096` causes truncation. The model's internal reasoning consumes the token budget, leaving nothing for the response.

**Fix:** The engine uses `max_tokens=8192`. If you've modified this, restore it. Check worker logs:

```bash
grep "iteration" /tmp/agentic-coder-*/worker-r1.log | tail -5
# If you see 30/30, the worker hit the cap
```

### "chat content is empty (2013)" error

**Cause:** MiniMax rejects `content: ""` when `tool_calls` are present.

**Fix:** The engine handles this by omitting the `content` field entirely. If you see this error, your proxy may be injecting an empty content field.

### Workers can't find files

**Cause:** Wrong workspace path, or the worker's `cwd` doesn't match.

**Fix:** Verify the workspace exists and is the correct project root:

```bash
ls /path/to/workspace/  # Should show project files
```

### Connection refused to MiniMax API

**Cause:** Proxy not running or wrong `MINIMAX_API_BASE`.

**Fix:**
```bash
# Check if proxy is running
curl -s http://localhost:4000/v1/models

# If not, start it
cd ~/codex-minimax-proxy && npm start
```

### Coordinator returns empty plan

**Cause:** API key invalid, model name wrong, or proxy error.

**Fix:**
```bash
# Test the API directly
curl -s "$MINIMAX_API_BASE/v1/chat/completions" \
  -H "Authorization: Bearer $MINIMAX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"MiniMax-M2.7","messages":[{"role":"user","content":"List 3 animals"}],"max_tokens":100}'
```

### Scratch dir fills up disk

**Fix:** Clean up old runs:

```bash
# Remove runs older than 7 days
find /tmp -maxdepth 1 -name "agentic-coder-*" -mtime +7 -exec rm -rf {} +

# Or add to cron
echo "0 4 * * * find /tmp -maxdepth 1 -name 'agentic-coder-*' -mtime +7 -exec rm -rf {} +" | crontab -
```

### Phase blocked but no error

**Cause:** Coordinator produced output that couldn't be parsed into worker tasks.

**Fix:** Read the coordinator's raw output:

```python
for phase in result.phases:
    if phase.status == "blocked":
        print(f"Phase {phase.phase} blocked. Coordinator said:")
        print(phase.coordinator_output[:1000])
```

---

## Advanced Usage

### Custom Scratch Directory for Persistence

By default scratch dirs go to `/tmp` and may be cleaned up by the OS. To persist:

```bash
export HERMES_AGENTIC_CODER_DIR="$HOME/.hermes/agentic-coder-runs"
mkdir -p "$HERMES_AGENTIC_CODER_DIR"
```

### Using Anthropic as Coordinator

The coordinator can use Anthropic instead of MiniMax (if you have a key):

```bash
python3 ~/hermes-evo/agentic_coder/cli.py \
  "Complex refactor requiring deep reasoning" \
  -w ~/my-project \
  --coordinator-model anthropic
```

Workers still use MiniMax regardless of this setting.

### Programmatic Result Processing

```python
import json

result = coder.run("...")

# Write structured result for downstream processing
output = {
    "goal": "...",
    "final_status": result.final_status,
    "scratch_dir": result.scratch_dir,
    "spec": result.spec,
    "phases": [
        {
            "phase": p.phase,
            "status": p.status,
            "worker_ids": p.worker_ids,
            "coordinator_output_preview": p.coordinator_output[:500],
        }
        for p in result.phases
    ],
}

with open("/tmp/agentic-coder-result.json", "w") as f:
    json.dump(output, f, indent=2)
```

### Integrating with Git Workflows

```bash
#!/bin/bash
# run-agentic-coder.sh — Run on a branch, review, and PR

GOAL="$1"
WORKSPACE="$2"
BRANCH="agentic-coder/$(date +%Y%m%d-%H%M%S)"

cd "$WORKSPACE"
git checkout -b "$BRANCH"

python3 ~/hermes-evo/agentic_coder/cli.py "$GOAL" -w "$WORKSPACE" -o /tmp/ac-result.json

STATUS=$(python3 -c "import json; print(json.load(open('/tmp/ac-result.json'))['final_status'])")

if [ "$STATUS" = "ready" ]; then
    git add -A
    git commit -m "agentic-coder: $GOAL"
    git push -u origin "$BRANCH"
    gh pr create --title "agentic-coder: $GOAL" --body "Automated changes by Agentic Coder"
    echo "PR created on branch $BRANCH"
else
    echo "Agentic Coder needs more work. Check /tmp/ac-result.json"
    git checkout main
    git branch -D "$BRANCH"
fi
```

### Parallel Runs on Multiple Projects

```python
import concurrent.futures
import sys, os
sys.path.insert(0, os.path.expanduser("~/hermes-evo/agentic_coder"))
from engine import AgenticCoder, AgenticCoderConfig

projects = [
    {"workspace": "~/project-a", "goal": "Add logging to all API handlers"},
    {"workspace": "~/project-b", "goal": "Fix deprecation warnings"},
    {"workspace": "~/project-c", "goal": "Update dependencies"},
]

def run_coder(task):
    config = AgenticCoderConfig(
        workspace=os.path.expanduser(task["workspace"]),
        max_workers=2,
        timeout_per_worker_minutes=10,
    )
    return AgenticCoder(config).run(task["goal"])

with concurrent.futures.ProcessPoolExecutor(max_workers=3) as executor:
    futures = {executor.submit(run_coder, t): t for t in projects}
    for future in concurrent.futures.as_completed(futures):
        task = futures[future]
        result = future.result()
        print(f"[{result.final_status}] {task['workspace']}: {task['goal']}")
```
