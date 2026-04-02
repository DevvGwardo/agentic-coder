---
name: agentic-coder
description: Four-phase autonomous coding agent — all MiniMax, no Anthropic. Coordinator plans, MiniMax subagents implement.
version: 1.1.0
author: Hermes Agent
tags: [coding, multi-agent, minimax, autonomous]
---

# Agentic Coder

A four-phase autonomous coding agent. All LLM calls go to MiniMax-M2.7.
The Coordinator (MiniMax) plans and orchestrates. Workers are detached
Python subprocess agents that call MiniMax directly via function calling.

## Architecture

```
User Goal
    │
    ▼
┌─────────────────────────────────────────────┐
│  Phase 1: Research                          │
│  Coordinator (MiniMax) → plan.md           │
│    → N × worker subprocesses (MiniMax)      │
│    → worker-*-research.md                  │
├─────────────────────────────────────────────┤
│  Phase 2: Synthesis                         │
│  Coordinator pre-reads findings → spec.md   │
├─────────────────────────────────────────────┤
│  Phase 3: Implementation                    │
│  Coordinator pre-reads spec → impl-plan.md  │
│    → N × worker subprocesses (MiniMax)       │
│    → worker-*-implementation.md             │
├─────────────────────────────────────────────┤
│  Phase 4: Verification                      │
│  Coordinator pre-reads reports → verified    │
└─────────────────────────────────────────────┘
Result: final_status = "ready" | "needs_work"
```

## Usage

**From Python:**
```python
from hermes_evo.agentic_coder import AgenticCoder, AgenticCoderConfig

coder = AgenticCoder(AgenticCoderConfig(
    workspace="/path/to/project",
    max_workers=3,
    verbose=True,
))
result = coder.run("Add rate limiting to the proxy endpoints")
print(result.final_status, result.spec)
```

**From terminal:**
```bash
python ~/hermes-evo/agentic_coder/cli.py \
  "Refactor auth to support OAuth2" \
  --workspace ~/codex-minimax-proxy \
  --max-workers 3 \
  --output /tmp/coder-result.json
```

**From Hermes (heartbeat/cron):**
```python
from hermes_evo.agentic_coder import AgenticCoder, AgenticCoderConfig

coder = AgenticCoder(AgenticCoderConfig(
    workspace="/Users/devgwardo/codex-minimax-proxy",
    scratch_dir="/tmp/agentic-coder-test",
    max_workers=3,
))
result = coder.run(user_goal)
# result.final_status → "ready" or "needs_work"
# result.scratch_dir  → where all outputs live
```

## Config fields

| Field | Default | Description |
|---|---|---|
| `workspace` | required | Project directory |
| `scratch_dir` | auto | Working directory for plans/reports |
| `max_workers` | 3 | Parallel workers per phase |
| `coordinator_model` | `MiniMax-M2.7` | Model for coordinator reasoning |
| `worker_model` | `MiniMax-M2.7` | Model for workers (same for now) |
| `verbose` | `True` | Timestamped logs to stdout |
| `timeout_per_worker_minutes` | 10 | Kill worker after this |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MINIMAX_API_KEY` | — | MiniMax API key |
| `MINIMAX_API_BASE` | `http://localhost:4000` | MiniMax proxy |
| `HERMES_AGENTIC_CODER_DIR` | `/tmp` | Parent for auto-generated scratch dirs |

## Scratch dir contents

After a run, `scratch_dir` contains:
- `plan.md` — coordinator's research plan
- `worker-*-research.md` — findings from research workers
- `spec.md` — the implementation specification
- `impl-plan.md` — coordinator's implementation plan
- `worker-*-implementation.md` — reports from implementation workers
- `verification.md` — final verification report
- `worker-*.log` — worker subprocess stdout

## Operational Notes — MiniMax-Specific Quirks

These are implementation details that cost real debugging time to discover.
**Read before touching the engine.**

### 1. `reasoning_details` instead of `content`

MiniMax sometimes returns the actual response text in `reasoning_details[0].text`
rather than `message.content`. This manifests as empty coordinator outputs and
workers that appear to do nothing.

**Fix:** Always check `reasoning_details` as a fallback:

```python
content = msg.get("content", "").strip()
if not content and msg.get("reasoning_details"):
    rd = msg["reasoning_details"]
    if isinstance(rd, list) and len(rd) > 0:
        content = rd[0].get("text", "").strip()
```

### 2. Finish reason `length` — response truncated

When `max_tokens` is too low, MiniMax's internal reasoning consumes the entire
budget and `content` comes back empty. Coordinator calls need higher limits.

**Fix:** Coordinator `max_tokens=8192`, workers `max_tokens=8192`. 4096 is too low.

### 3. `content: ''` rejected when `tool_calls` present

If an assistant message has tool calls but no text, MiniMax rejects it with
`"chat content is empty (2013)"`. The `content` field must be omitted entirely,
not sent as empty string.

**Fix:** When appending assistant messages with tool calls:
```python
if assistant_text:
    messages.append({"role": "assistant", "content": assistant_text})
elif tool_calls:
    # No text, tools only — omit content field
    messages.append({"role": "assistant", "tool_calls": tool_calls})
```

### 4. System-only + tools → HTTP 400

MiniMax requires at least one user message when function calling is enabled.
System-only messages with tools get rejected.

**Fix:** Workers start with `[{role: system}, {role: user}]` — a dummy user
message is required to activate tool calling.

### 5. Nested triple backticks in f-strings

Python 3.9 f-strings cannot contain `"""`. Any prompt function that builds
multi-line strings with code fences must use `"".join([...])` or single-tick
delimiters, not triple-quoted f-strings.

**Fix:** Build prompts as `"\n".join([line1, line2, ...])`.

### 6. Tool result truncation at 4096 tokens

Workers making multiple tool calls accumulate long context. At 4096 `max_tokens`,
responses get cut off before the model processes tool results — the agent loops
indefinitely on the same tool.

**Fix:** `max_tokens=8192` for workers. This was the cause of workers hitting
the iteration cap without making progress.

## Worker Tool Schema

Workers have native function calling. Available tools:

| Tool | Purpose |
|---|---|
| `read_file` | Read file content (previews at 500 chars) |
| `write_file` | Write or overwrite a file |
| `list_dir` | List directory entries |
| `shell` | Run shell command, returns stdout/stderr/rc |
| `path_exists` | Check if path exists and what type |

## Open Issues

- Implementation workers occasionally hit iteration cap before writing reports.
  A budget-nudging system (inject "write now" prompt in last 3 iterations)
  would unblock this without restructuring the loop.
- Verification phase uses coordinator without pre-read — same pattern as
  synthesis but fix not yet propagated.
- Workers use `MAX_ITERATIONS=30` — sufficient for exploration, may still be
  tight for complex multi-file changes.
