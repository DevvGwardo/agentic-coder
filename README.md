# Agentic Coder

A four-phase autonomous coding agent. All LLM calls go to MiniMax-M2.7.
The Coordinator plans and orchestrates. Workers are detached Python subprocess agents that call MiniMax directly via function calling.

## Architecture

```
User Goal
    |
    v
+---------------------------------------------+
|  Phase 1: Research                          |
|  Coordinator (MiniMax) -> plan.md           |
|    -> N x worker subprocesses (MiniMax)     |
|    -> worker-*-research.md                  |
+---------------------------------------------+
|  Phase 2: Synthesis                         |
|  Coordinator pre-reads findings -> spec.md  |
+---------------------------------------------+
|  Phase 3: Implementation                    |
|  Coordinator pre-reads spec -> impl-plan.md |
|    -> N x worker subprocesses (MiniMax)     |
|    -> worker-*-implementation.md            |
+---------------------------------------------+
|  Phase 4: Verification                      |
|  Coordinator pre-reads reports -> verified  |
+---------------------------------------------+
Result: final_status = "ready" | "needs_work"
```

## Quick Start

### Prerequisites

- Python 3.9+
- A MiniMax API key (or a proxy running at `localhost:4000`)

### Environment Setup

```bash
export MINIMAX_API_KEY="your-api-key"
export MINIMAX_API_BASE="http://localhost:4000"  # optional, defaults to this
```

### CLI Usage

```bash
python cli.py \
  "Refactor auth to support OAuth2" \
  --workspace ~/my-project \
  --max-workers 3 \
  --output /tmp/coder-result.json
```

### Python Usage

```python
from agentic_coder import AgenticCoder, AgenticCoderConfig

coder = AgenticCoder(AgenticCoderConfig(
    workspace="/path/to/project",
    max_workers=3,
    verbose=True,
))
result = coder.run("Add rate limiting to the proxy endpoints")
print(result.final_status, result.spec)
```

## Configuration

| Field | Default | Description |
|---|---|---|
| `workspace` | required | Project directory |
| `scratch_dir` | auto | Working directory for plans/reports |
| `max_workers` | 3 | Parallel workers per phase |
| `coordinator_model` | `MiniMax-M2.7` | Model for coordinator reasoning |
| `worker_model` | `MiniMax-M2.7` | Model for workers |
| `verbose` | `True` | Timestamped logs to stdout |
| `timeout_per_worker_minutes` | 10 | Kill worker after this |

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `MINIMAX_API_KEY` | -- | MiniMax API key |
| `MINIMAX_API_BASE` | `http://localhost:4000` | MiniMax proxy URL |
| `HERMES_AGENTIC_CODER_DIR` | `/tmp` | Parent for auto-generated scratch dirs |

## How It Works

1. **Research** -- The coordinator creates a research plan, then spawns N worker subprocesses that investigate the codebase in parallel. Each worker writes findings to `worker-*-research.md`.

2. **Synthesis** -- The coordinator reads all research findings and produces a `spec.md` -- a detailed implementation specification.

3. **Implementation** -- The coordinator reads the spec and creates an implementation plan. Workers execute targeted changes in parallel, each reporting to `worker-*-implementation.md`.

4. **Verification** -- The coordinator reviews all implementation reports and verifies changes against the spec. Final status is `"ready"` or `"needs_work"`.

## Worker Tools

Workers have native MiniMax function calling with these tools:

| Tool | Purpose |
|---|---|
| `read_file` | Read file content (previews at 500 chars) |
| `write_file` | Write or overwrite a file |
| `list_dir` | List directory entries |
| `shell` | Run shell command, returns stdout/stderr/rc |
| `path_exists` | Check if path exists and what type |

## Scratch Directory Output

After a run, `scratch_dir` contains:

```
scratch_dir/
  plan.md                       # coordinator's research plan
  worker-*-research.md          # findings from research workers
  spec.md                       # the implementation specification
  impl-plan.md                  # coordinator's implementation plan
  worker-*-implementation.md    # reports from implementation workers
  verification.md               # final verification report
  worker-*.log                  # worker subprocess stdout
```

## MiniMax-Specific Quirks

Hard-won lessons from debugging MiniMax-M2.7 integration. Read before touching the engine.

| Issue | Symptom | Fix |
|---|---|---|
| `reasoning_details` fallback | Empty coordinator outputs | Check `reasoning_details[0].text` when `content` is empty |
| Token budget exhaustion | Truncated responses | Use `max_tokens=8192` (4096 is too low) |
| Empty content with tool calls | `"chat content is empty (2013)"` error | Omit `content` field entirely, don't send `""` |
| System-only + tools | HTTP 400 | Always include at least one user message |
| Nested triple backticks | Python 3.9 f-string syntax error | Build prompts with `"\n".join([...])` |
| Tool result accumulation | Workers loop indefinitely | `max_tokens=8192` prevents truncation |

## Known Issues

- Implementation workers occasionally hit the iteration cap (30) before writing reports
- Verification phase doesn't pre-read worker outputs (synthesis pattern not yet propagated)
- Complex multi-file changes may need a higher iteration cap

## License

MIT
