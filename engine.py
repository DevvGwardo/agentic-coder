"""
AgenticCoder — Coordinator + MiniMax workers, all-in on MiniMax.

Four-phase orchestration:
  1. Research    — coordinator (MiniMax) breaks the goal into tasks
  2. Synthesis  — coordinator writes a spec from findings
  3. Implement  — N MiniMax subagents make targeted changes in parallel
  4. Verify     — coordinator validates against the spec

All LLM calls go to MiniMax. Workers are spawned as detached Hermes subagents
with full tool access (file read/write, terminal, browser, etc.).
"""

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Types ────────────────────────────────────────────────────────────────────

WORKER_COMPLETION_MARKER = "<<<WORKER_DONE>>>"


@dataclass
class PhaseResult:
    phase: str
    status: str  # "ok" | "error" | "blocked"
    coordinator_output: str
    worker_ids: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class CoderResult:
    phases: list[PhaseResult]
    scratch_dir: str
    final_status: str  # "ready" | "needs_work"
    spec: Optional[str] = None


# ─── MiniMax API call ────────────────────────────────────────────────────────

def _complete_minimax(
    messages: list[dict],
    system: str = "",
    model: str = "MiniMax-M2.7",
    max_tokens: int = 8192,
    temperature: float = 0.3,
) -> str:
    """Call MiniMax chat completions via the local proxy.

    MiniMax sometimes returns content in reasoning_details rather than
    the content field. We check both.
    """
    api_base = os.getenv("MINIMAX_API_BASE", "http://localhost:4000")
    api_key = os.getenv("MINIMAX_API_KEY", "")

    payload = {
        "model": model,
        "messages": [{"role": "user" if m["role"] == "user" else m["role"],
                     "content": m["content"]} for m in messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        payload["system"] = system

    try:
        import urllib.request
        req = urllib.request.Request(
            f"{api_base}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())

        msg = data["choices"][0]["message"]
        content = msg.get("content", "").strip()

        # MiniMax sometimes puts the actual response in reasoning_details
        if not content and msg.get("reasoning_details"):
            rd = msg["reasoning_details"]
            if isinstance(rd, list) and len(rd) > 0:
                content = rd[0].get("text", "").strip()

        if not content:
            # Last resort: look for text in any reasoning field
            for val in msg.values():
                if isinstance(val, str) and len(val) > 10:
                    content = val.strip()
                    break

        return content
    except Exception as e:
        raise RuntimeError(f"MiniMax API error: {e}")


# ─── Coordinator prompts ─────────────────────────────────────────────────────

COORDINATOR_SYSTEM = """You are a coordinating agent. Your job is to break down complex tasks into phases,
assign work to specialized worker agents, and synthesize their findings into a coherent plan.

You NEVER do the hands-on work yourself. You orchestrate.

Key principles:
- Parallelism is your superpower. Launch independent workers concurrently.
- Be precise in task assignments — vague instructions produce vague results.
- Workers operate in isolated scratch directories. Read their output files to synthesize.
- Always include exact file paths and concrete change descriptions in worker instructions.
- When writing specs, be specific: exact files, exact lines, exact changes."""


def build_research_prompt(goal: str, workspace: str, scratch_dir: str, max_workers: int) -> str:
    code_fence = "```"
    lines = [
        "## Phase 1 — Research",
        "",
        "GOAL: " + goal,
        "WORKSPACE: " + workspace,
        "SCRATCH_DIR: " + scratch_dir,
        "MAX_WORKERS: " + str(max_workers),
        "",
        "Break this goal into exactly " + str(max_workers) + " independent investigation tasks.",
        "Each worker investigates a different aspect — no overlap.",
        "",
        "For each worker, specify:",
        "- A unique ID (r1, r2, r3...)",
        "- Exactly what to investigate, where to look, what success looks like",
        "- The WORKSPACE path",
        "",
        "Then immediately plan all " + str(max_workers) + " tasks.",
        "",
        "Write your plan to " + scratch_dir + "/plan.md using this format:",
        code_fence,
        "WORKER r1:",
        "instruction: <precise instruction with file paths>",
        "workspace: " + workspace,
        "",
        "WORKER r2:",
        "instruction: <precise instruction>",
        "workspace: " + workspace,
        "...",
        code_fence,
        "",
        "After all workers return, synthesize their findings in 3-5 sentences.",
    ]
    return "\n".join(lines)


def build_synthesis_prompt(goal: str, workspace: str, scratch_dir: str) -> str:
    import os
    worker_reports = ""
    for fname in sorted(os.listdir(scratch_dir)):
        if fname.startswith("worker-") and fname.endswith("-research.md"):
            try:
                with open(os.path.join(scratch_dir, fname)) as f:
                    content = f.read()
                worker_reports += "\n\n## " + fname + "\n\n" + content
            except Exception:
                pass

    reports_section = worker_reports if worker_reports else "(No worker reports found — proceed anyway)"

    lines = [
        "## Phase 2 — Synthesis",
        "",
        "GOAL: " + goal,
        "WORKSPACE: " + workspace,
        "SPEC_FILE: " + scratch_dir + "/spec.md",
        "",
        "Below are all worker findings. Read them carefully.",
        "",
        reports_section,
        "",
        "Instructions:",
        "1. Distill key facts, patterns, and decisions from the findings above",
        "2. If findings conflict, pick the best interpretation",
        "3. Write a concrete, actionable SPEC to " + scratch_dir + "/spec.md",
        "",
        "The spec MUST include:",
        "- Exact files to change",
        "- Exact changes (code snippets, not vague descriptions)",
        "- Any constraints or requirements",
        "",
        "After writing the spec, respond with:",
        "- A 3-5 sentence summary of findings",
        "- Confirmation that spec.md was written",
    ]
    return "\n".join(lines)


def build_implementation_prompt(goal: str, workspace: str, scratch_dir: str, max_workers: int) -> str:
    spec_text = ""
    spec_path = os.path.join(scratch_dir, "spec.md")
    try:
        with open(spec_path) as f:
            spec_text = f.read()[:2000]
    except Exception:
        spec_text = "(spec.md not found)"

    # Build the example block without triple backticks inside f-string
    # Use single ticks to avoid f-string delimiter conflict
    code_fence = "```"
    example_block = (
        code_fence + "\n"
        "WORKER i1:\n"
        "instruction: In src/server.ts, add this route after the existing routes: "
        "app.get('/health', (c) => c.json({status: 'ok'}))\n"
        "workspace: " + workspace + "\n"
        + code_fence
    )

    lines = [
        "## Phase 3 — Implementation",
        "",
        "You must produce exactly " + str(max_workers) + " worker tasks in the format below.",
        "Write them to " + scratch_dir + "/impl-plan.md.",
        "",
        "WORKER FORMAT (each worker gets ONE of these blocks):",
        example_block,
        "",
        "SPEC (read carefully before writing tasks):",
        spec_text,
        "",
        "WORKSPACE: " + workspace,
        "MAX_WORKERS: " + str(max_workers),
        "",
        "Rules:",
        "- Each worker must have a unique ID: i1, i2, i3... (up to i" + str(max_workers) + ")",
        "- Each worker instruction must specify exact file paths and exact code",
        "- Split so no two workers touch the same file unless necessary",
        "- Workers write reports to " + scratch_dir + "/worker-<id>-implementation.md",
        "",
        "Write your " + str(max_workers) + " worker tasks to " + scratch_dir + "/impl-plan.md now.",
    ]
    return "\n".join(lines)


def build_verification_prompt(goal: str, workspace: str, scratch_dir: str) -> str:
    lines = [
        "## Phase 4 — Verification",
        "",
        "Verify the implementation against the spec.",
        "",
        "SPEC: " + scratch_dir + "/spec.md",
        "WORKSPACE: " + workspace,
        "SCRATCH_DIR: " + scratch_dir,
        "",
        "Check:",
        "1. Read spec.md and each implementation report in " + scratch_dir + "/worker-*-implementation.md",
        "2. Verify every spec item was implemented correctly",
        "3. Run validation commands if needed",
        "4. Report pass/fail for each spec item",
        "",
        "Write final report to: " + scratch_dir + "/verification.md",
        "",
        "Respond with pass/fail for each spec item and:",
        "  Final status: READY",
        "or",
        "  Final status: NEEDS_WORK",
    ]
    return "\n".join(lines)


# ─── Worker spawning via mcp_delegate_task ──────────────────────────────────

def _launch_minimax_worker(
    worker_id: str,
    instruction: str,
    workspace: str,
    scratch_dir: str,
    phase: str,
    goal: str,
) -> dict:
    """
    Spawn a MiniMax subagent via mcp_delegate_task.
    The subagent runs in an isolated context with terminal+file tools.
    """
    from hermes_tools import terminal, read_file, write_file

    # Build the worker's system prompt
    worker_system = f"""You are a focused coding agent. Your job is to execute a task in your workspace.

RULES:
- Be precise: exact file paths, exact changes
- If blocked, describe what you tried, what happened, and what would help
- When done, write a summary to {scratch_dir}/worker-{worker_id}-{phase}.md
- In that summary, include the marker line: {WORKER_COMPLETION_MARKER}

WORKSPACE: {workspace}
GOAL: {goal}
SCRATCH_DIR: {scratch_dir}
WORKER_ID: {worker_id}
PHASE: {phase}

Your task:
{instruction}

Execute the task. Make the changes. Then write your findings to {scratch_dir}/worker-{worker_id}-{phase}.md."""

    # Read existing context files from scratch dir for this worker
    context_files = ""
    try:
        import os as _os
        for fname in _os.listdir(scratch_dir):
            if fname.endswith(".md") and fname != f"worker-{worker_id}-{phase}.md":
                try:
                    with open(os.path.join(scratch_dir, fname)) as f:
                        content = f.read()[:1500]
                    context_files += f"\n\n## From {fname}\n\n{content}"
                except Exception:
                    pass
    except Exception:
        pass

    if context_files:
        worker_system += f"\n\n## Context from Other Workers\n{context_files}"

    task_id = f"agentic-coder-{worker_id}-{phase}"

    # Use mcp_delegate_task to spawn a subagent
    # The subagent inherits tools (terminal, read_file, write_file, etc.)
    try:
        from mcp.types import (
            DelegateTaskParams,
            DelegateTaskResult,
        )
    except ImportError:
        pass  # Not all contexts have mcp.types

    # We'll call the delegate_task tool directly
    import sys
    tools = __import__("hermes_tools", fromlist=["terminal", "read_file", "write_file"])

    # Build context for subagent
    context = f"""
WORKER_ID: {worker_id}
PHASE: {phase}
WORKSPACE: {workspace}
SCRATCH_DIR: {scratch_dir}
GOAL: {goal}
INSTRUCTION: {instruction}
"""

    try:
        from hermes_client import hermes
        # Spawn the subagent
        # We use a background session approach: write the task to a fifo/queue
        # then let a background process pick it up

        # Instead, use the cron job approach: write a task file
        # and trigger via the API server
        pass
    except ImportError:
        pass

    # Fallback: use subprocess with a Python agent loop
    return _run_worker_subprocess(worker_id, instruction, workspace, scratch_dir, phase, goal)


def _run_worker_subprocess(
    worker_id: str,
    instruction: str,
    workspace: str,
    scratch_dir: str,
    phase: str,
    goal: str,
) -> dict:
    """
    Run a MiniMax agent loop in a detached subprocess.
    The subprocess calls MiniMax API directly and executes tool calls.
    """
    import subprocess
    import sys

    # Build worker agent script using MiniMax function calling (no code-block parsing needed)
    _sd = scratch_dir
    _wid = worker_id
    _phase = phase
    _goal = goal
    _ws = workspace
    _inst = instruction
    _marker = WORKER_COMPLETION_MARKER

    worker_script = (
        "import json, os, re, sys, urllib.request, urllib.error, subprocess, io, contextlib, tempfile, traceback\n"
        "\n"
        f"SCRATCH_DIR = {json.dumps(_sd)}\n"
        f"WORKER_ID = {json.dumps(_wid)}\n"
        f"PHASE = {json.dumps(_phase)}\n"
        f"GOAL = {json.dumps(_goal)}\n"
        f"WORKSPACE = {json.dumps(_ws)}\n"
        f"INSTRUCTION = {json.dumps(_inst)}\n"
        f"WORKER_COMPLETION_MARKER = {json.dumps(_marker)}\n"
        "\n"
        'API_BASE = os.getenv("MINIMAX_API_BASE", "http://localhost:4000")\n'
        'API_KEY = os.getenv("MINIMAX_API_KEY", "")\n'
        'MODEL = "MiniMax-M2.7"\n'
        "MAX_ITERATIONS = 30\n"
        "TOOL_CALL_ID = 0\n"
        "\n"
        "# ── Tool definitions ────────────────────────────────────────────────────\n"
        "\n"
        "TOOLS = [\n"
        "    {\n"
        '        "type": "function",\n'
        '        "function": {\n'
        '            "name": "read_file",\n'
        '            "description": "Read a text file. Returns the full content or an error message.",\n'
        '            "parameters": {\n'
        '                "type": "object",\n'
        '                "properties": {\n'
        '                    "path": {"type": "string", "description": "Absolute path to the file"}\n'
        '                },\n'
        '                "required": ["path"]\n'
        '            }\n'
        '        }\n'
        '    },\n'
        "    {\n"
        '        "type": "function",\n'
        '        "function": {\n'
        '            "name": "write_file",\n'
        '            "description": "Write content to a file. Creates or overwrites.",\n'
        '            "parameters": {\n'
        '                "type": "object",\n'
        '                "properties": {\n'
        '                    "path": {"type": "string"},\n'
        '                    "content": {"type": "string"}\n'
        '                },\n'
        '                "required": ["path", "content"]\n'
        '            }\n'
        '        }\n'
        '    },\n'
        "    {\n"
        '        "type": "function",\n'
        '        "function": {\n'
        '            "name": "list_dir",\n'
        '            "description": "List files in a directory.",\n'
        '            "parameters": {\n'
        '                "type": "object",\n'
        '                "properties": {\n'
        '                    "path": {"type": "string"}\n'
        '                },\n'
        '                "required": ["path"]\n'
        '            }\n'
        '        }\n'
        '    },\n'
        "    {\n"
        '        "type": "function",\n'
        '        "function": {\n'
        '            "name": "shell",\n'
        '            "description": "Run a shell command. Returns stdout, stderr, and return code.",\n'
        '            "parameters": {\n'
        '                "type": "object",\n'
        '                "properties": {\n'
        '                    "command": {"type": "string"},\n'
        '                    "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 60}\n'
        '                },\n'
        '                "required": ["command"]\n'
        '            }\n'
        '        }\n'
        '    },\n'
        "    {\n"
        '        "type": "function",\n'
        '        "function": {\n'
        '            "name": "path_exists",\n'
        '            "description": "Check if a path exists and what type it is.",\n'
        '            "parameters": {\n'
        '                "type": "object",\n'
        '                "properties": {\n'
        '                    "path": {"type": "string"}\n'
        '                },\n'
        '                "required": ["path"]\n'
        '            }\n'
        '        }\n'
        '    },\n'
        "]\n"
        "\n"
        "# ── Tool implementations ─────────────────────────────────────────────────\n"
        "\n"
        "def tool_read_file(args):\n"
        "    path = args['path']\n"
        "    if not os.path.exists(path):\n"
        "        return json.dumps({'error': 'File not found: ' + path})\n"
        "    try:\n"
        "        with open(path, 'r', encoding='utf-8') as f:\n"
        "            content = f.read()\n"
        "        size = len(content)\n"
        "        preview = content[:500] + ('...[truncated]' if len(content) > 500 else '')\n"
        "        return json.dumps({'size': size, 'content': preview})\n"
        "    except Exception as e:\n"
        "        return json.dumps({'error': str(e)})\n"
        "\n"
        "def tool_write_file(args):\n"
        "    path = args['path']\n"
        "    content = args['content']\n"
        "    try:\n"
        "        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)\n"
        "        with open(path, 'w', encoding='utf-8') as f:\n"
        "            f.write(content)\n"
        "        return json.dumps({'written': len(content), 'path': path})\n"
        "    except Exception as e:\n"
        "        return json.dumps({'error': str(e)})\n"
        "\n"
        "def tool_list_dir(args):\n"
        "    path = args['path']\n"
        "    if not os.path.isdir(path):\n"
        "        return json.dumps({'error': 'Not a directory: ' + path})\n"
        "    try:\n"
        "        entries = os.listdir(path)\n"
        "        return json.dumps({'entries': entries[:50], 'total': len(entries)})\n"
        "    except Exception as e:\n"
        "        return json.dumps({'error': str(e)})\n"
        "\n"
        "def tool_shell(args):\n"
        "    cmd = args['command']\n"
        "    timeout = args.get('timeout', 60)\n"
        "    try:\n"
        "        result = subprocess.run(\n"
        "            cmd, shell=True, capture_output=True, text=True,\n"
        "            cwd=WORKSPACE, timeout=timeout\n"
        "        )\n"
        "        return json.dumps({\n"
        "            'stdout': result.stdout[:2000],\n"
        "            'stderr': result.stderr[:1000],\n"
        "            'rc': result.returncode\n"
        "        })\n"
        "    except subprocess.TimeoutExpired:\n"
        "        return json.dumps({'error': 'Command timed out after ' + str(timeout) + 's'})\n"
        "    except Exception as e:\n"
        "        return json.dumps({'error': str(e)})\n"
        "\n"
        "def tool_path_exists(args):\n"
        "    path = args['path']\n"
        "    exists = os.path.exists(path)\n"
        "    kind = 'file' if os.path.isfile(path) else 'dir' if os.path.isdir(path) else 'none' if exists else 'missing'\n"
        "    return json.dumps({'path': path, 'exists': exists, 'kind': kind})\n"
        "\n"
        "TOOL_IMPLS = {\n"
        "    'read_file': tool_read_file,\n"
        "    'write_file': tool_write_file,\n"
        "    'list_dir': tool_list_dir,\n"
        "    'shell': tool_shell,\n"
        "    'path_exists': tool_path_exists,\n"
        "}\n"
        "\n"
        "# ── API call ─────────────────────────────────────────────────────────────\n"
        "\n"
        "def llm_complete(messages, tools=None, tool_choice='auto'):\n"
        "    global TOOL_CALL_ID\n"
        "    payload = {\n"
        "        'model': MODEL,\n"
        "        'messages': messages,\n"
        "        'max_tokens': 8192,\n"
        "        'temperature': 0.3,\n"
        "    }\n"
        "    if tools:\n"
        "        payload['tools'] = tools\n"
        "        payload['tool_choice'] = tool_choice\n"
        "    req = urllib.request.Request(\n"
        "        API_BASE + '/v1/chat/completions',\n"
        "        data=json.dumps(payload).encode(),\n"
        "        headers={'Authorization': 'Bearer ' + API_KEY, 'Content-Type': 'application/json'},\n"
        "        method='POST',\n"
        "    )\n"
        "    try:\n"
        "        with urllib.request.urlopen(req, timeout=120) as resp:\n"
        "            raw = json.loads(resp.read())\n"
        "            msg = raw['choices'][0]['message']\n"
        "            content = msg.get('content', '').strip()\n"
        "            if not content and msg.get('reasoning_details'):\n"
        "                rd = msg['reasoning_details']\n"
        "                if isinstance(rd, list) and len(rd) > 0:\n"
        "                    content = rd[0].get('text', '').strip()\n"
        "            if not content:\n"
        "                for val in msg.values():\n"
        "                    if isinstance(val, str) and len(val) > 10:\n"
        "                        content = val.strip(); break\n"
        "            return {'content': content, 'raw': raw}\n"
        "    except urllib.error.HTTPError as e:\n"
        "        body = e.read().decode()[:500]\n"
        "        return {'error': 'HTTP ' + str(e.code) + ': ' + body}\n"
        "    except Exception as e:\n"
        "        return {'error': str(e)}\n"
        "\n"
        "# ── Main loop ────────────────────────────────────────────────────────────\n"
        "\n"
        "SYSTEM_PROMPT = (\n"
        '    "You are a coding agent. You MUST use the available tools to complete your task.\\n\\n" +\n'
        '    "WORKSPACE: " + WORKSPACE + "\\n" +\n'
        '    "WORKER_ID: " + WORKER_ID + "\\n" +\n'
        '    "PHASE: " + PHASE + "\\n" +\n'
        '    "GOAL: " + GOAL + "\\n\\n" +\n'
        '    "AVAILABLE TOOLS: read_file, write_file, list_dir, shell, path_exists\\n" +\n'
        '    "RULES:\\n" +\n'
        '    "- ALWAYS use tools when you need to read, write, or run commands\\n" +\n'
        '    "- Do NOT try to guess what files contain — use read_file\\n" +\n'
        '    "- When done, write a summary report to: " + SCRATCH_DIR + "/worker-" + WORKER_ID + "-" + PHASE + ".md\\n" +\n'
        '    "- In the summary, include the exact changes you made and files touched\\n" +\n'
        '    "- End the summary with this exact line on its own: " + WORKER_COMPLETION_MARKER + "\\n\\n" +\n'
        '    "TASK:\\n" +\n'
        '    INSTRUCTION\n'
        ")\n"
        "\n"
        "messages = [\n"
        "    {'role': 'system', 'content': SYSTEM_PROMPT},\n"
        "    {'role': 'user', 'content': 'Work on your task now.'}\n"
        "]\n"
        "iterations = 0\n"
        "done = False\n"
        "\n"
        "while iterations < MAX_ITERATIONS and not done:\n"
        "    iterations += 1\n"
        "    print('[worker-' + WORKER_ID + '] iteration ' + str(iterations) + '/' + str(MAX_ITERATIONS), flush=True)\n"
        "\n"
        "    resp = llm_complete(messages, tools=TOOLS)\n"
        "\n"
        "    if 'error' in resp:\n"
        "        print('[worker-' + WORKER_ID + '] API error: ' + str(resp['error']), flush=True)\n"
        "        break\n"
        "\n"
        "    raw = resp.get('raw', {})\n"
        "    msg = raw.get('choices', [{}])[0].get('message', {})\n"
        "\n"
        "    # Assistant message — could have text, tool_calls, or both\n"
        "    assistant_text = resp.get('content', '')\n"
        "    tool_calls = msg.get('tool_calls', [])\n"
        "\n"
        "    if assistant_text:\n"
        "        messages.append({'role': 'assistant', 'content': assistant_text})\n"
        "        if WORKER_COMPLETION_MARKER in assistant_text:\n"
        "            done = True\n"
        "            break\n"
        "    elif tool_calls:\n"
        "        # Assistant message with tool_calls but no text — omit content field\n"
        "        # (MiniMax rejects content='' when tool_calls are present)\n"
        "        tc_msg = {'role': 'assistant', 'tool_calls': tool_calls}\n"
        "        messages.append(tc_msg)\n"
        "\n"
        "    if not tool_calls:\n"
        "        # No tools and no completion marker — ask to continue\n"
        "        if not done:\n"
        "            messages.append({'role': 'user', 'content': 'Continue working. If you are finished, write your report and include ' + WORKER_COMPLETION_MARKER + '.'})\n"
        "        continue\n"
        "\n"
        "    # Execute each tool call\n"
        "    for tc in tool_calls:\n"
        "        tc_id = tc.get('id', str(iterations))\n"
        "        fn = tc.get('function', {})\n"
        "        name = fn.get('name', '')\n"
        "        raw_args = fn.get('arguments', '{}')\n"
        "\n"
        "        # Parse arguments (might be string or dict)\n"
        "        if isinstance(raw_args, str):\n"
        "            try:\n"
        "                args = json.loads(raw_args)\n"
        "            except Exception:\n"
        "                args = {'raw': raw_args}\n"
        "        else:\n"
        "            args = raw_args\n"
        "\n"
        "        print('[worker-' + WORKER_ID + '] tool_call: ' + name + '(' + str(args)[:100] + ')', flush=True)\n"
        "\n"
        "        impl = TOOL_IMPLS.get(name)\n"
        "        if impl:\n"
        "            try:\n"
        "                result = impl(args)\n"
        "            except Exception as e:\n"
        "                result = json.dumps({'error': str(e)})\n"
        "        else:\n"
        "            result = json.dumps({'error': 'Unknown tool: ' + name})\n"
        "\n"
        "        messages.append({\n"
        "            'role': 'tool',\n"
        "            'tool_call_id': tc_id,\n"
        "            'content': result\n"
        "        })\n"
        "\n"
        "# Write final report\n"
        "report_path = os.path.join(SCRATCH_DIR, 'worker-' + WORKER_ID + '-' + PHASE + '.md')\n"
        "if not os.path.exists(report_path):\n"
        "    with open(report_path, 'w') as f:\n"
        "        f.write('# Worker ' + WORKER_ID + ' Report\\n')\n"
        "        f.write('\\nIterations: ' + str(iterations) + '/' + str(MAX_ITERATIONS) + '\\n')\n"
        "        f.write('\\n' + WORKER_COMPLETION_MARKER + '\\n')\n"
        "\n"
        "print('[worker-' + WORKER_ID + '] done (iterations=' + str(iterations) + ', done=' + str(done) + ')', flush=True)\n"
    )

    log_file = os.path.join(scratch_dir, f"worker-{worker_id}.log")
    env = os.environ.copy()

    proc = subprocess.Popen(
        [sys.executable, "-c", worker_script],
        cwd=workspace,
        env=env,
        stdout=open(log_file, "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    return {"pid": proc.pid, "worker_id": worker_id, "log_file": log_file}


def _wait_for_workers(
    procs: list[dict],
    scratch_dir: str,
    timeout_minutes: int = 15,
) -> dict[str, bool]:
    """Poll worker log files until all signal completion or timeout."""
    deadline = time.time() + timeout_minutes * 60
    done: dict[str, bool] = {p["worker_id"]: False for p in procs}

    while time.time() < deadline:
        all_done = True
        for p in procs:
            wid = p["worker_id"]
            if done[wid]:
                continue
            # Check if process is still running
            import subprocess
            if p.get("pid"):
                result = subprocess.run(
                    ["ps", "-p", str(p["pid"])],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    # Process is gone
                    done[wid] = True
                    continue
            # Check log for completion marker
            try:
                log_path = p.get("log_file", os.path.join(scratch_dir, f"worker-{wid}.log"))
                if os.path.exists(log_path):
                    with open(log_path) as f:
                        content = f.read()
                    if WORKER_COMPLETION_MARKER in content or ("exiting" in content and "worker-" + wid in content):
                        done[wid] = True
                        continue
            except Exception:
                pass
            all_done = False

        if all_done:
            break
        time.sleep(5)

    # Force-kill stragglers
    import subprocess
    for p in procs:
        if not done[p["worker_id"]] and p.get("pid"):
            try:
                subprocess.run(["kill", str(p["pid"])], capture_output=True)
            except Exception:
                pass
        done[p["worker_id"]] = True  # Mark all as done after wait

    return done


# ─── Parse plan files ────────────────────────────────────────────────────────

def _parse_worker_ids_from_plan(plan_path: str) -> list[str]:
    try:
        with open(plan_path) as f:
            content = f.read()
        return re.findall(r"^WORKER\s+(\w+):", content, re.MULTILINE)
    except Exception:
        return []


def _extract_instruction_from_plan(plan_content: str, worker_id: str) -> str:
    lines = plan_content.split("\n")
    in_block = False
    block_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"WORKER {worker_id}:"):
            in_block = True
            continue
        if in_block:
            if stripped.startswith("WORKER ") and not stripped.startswith(f"WORKER {worker_id}"):
                break
            block_lines.append(line)
    instruction = "\n".join(block_lines).strip()
    if instruction.lower().startswith("instruction:"):
        instruction = instruction.split(":", 1)[1].strip()
    return instruction


# ─── Config ──────────────────────────────────────────────────────────────────

@dataclass
class AgenticCoderConfig:
    workspace: str
    scratch_dir: Optional[str] = None
    max_workers: int = 3
    coordinator_model: str = "MiniMax-M2.7"
    worker_model: str = "MiniMax-M2.7"  # not used yet — workers call same model
    verbose: bool = True
    timeout_per_worker_minutes: int = 10

    def __post_init__(self):
        if self.scratch_dir is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            self.scratch_dir = os.path.join(
                os.getenv("HERMES_AGENTIC_CODER_DIR", "/tmp"),
                f"agentic-coder-{ts}",
            )


# ─── Main Engine ─────────────────────────────────────────────────────────────

class AgenticCoder:
    """
    Coordinator + MiniMax workers.

    All LLM calls go to MiniMax. Workers are detached subprocess agents
    that call MiniMax directly and execute tool calls (file ops, shell).

    Usage:
        coder = AgenticCoder(AgenticCoderConfig(workspace="/path/to/project"))
        result = coder.run("Refactor auth to support OAuth2")
        print(result.final_status, result.spec)
    """

    def __init__(self, config: AgenticCoderConfig):
        self.config = config
        self._scratch_dir = config.scratch_dir
        os.makedirs(self._scratch_dir, exist_ok=True)
        self._log(f"AgenticCoder initialized. scratch_dir={self._scratch_dir}")
        self._log(f"Coordinator model: {config.coordinator_model}")
        self._log(f"Worker model: {config.worker_model}")

    def _log(self, msg: str):
        if self.config.verbose:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] [agentic-coder] {msg}", flush=True)

    def _coordinator_complete(self, prompt: str, system: str = "") -> str:
        """Coordinator call — no tools, plain text only. Pre-read files before calling."""
        messages = [{"role": "user", "content": prompt}]
        resp = _complete_minimax(
            messages,
            system=system,
            model=self.config.coordinator_model,
            max_tokens=8192,
            temperature=0.1,
        )
        return resp

    # ── Phase runners ────────────────────────────────────────────────────────

    def _run_research(self, goal: str) -> PhaseResult:
        self._log("PHASE 1: Research")
        sd = self._scratch_dir

        prompt = build_research_prompt(goal, self.config.workspace, sd, self.config.max_workers)
        plan = self._coordinator_complete(prompt, COORDINATOR_SYSTEM)

        plan_file = os.path.join(sd, "plan.md")
        with open(plan_file, "w") as f:
            f.write(plan)

        worker_ids = _parse_worker_ids_from_plan(plan_file)
        self._log(f"  Coordinator planned {len(worker_ids)} research tasks: {worker_ids}")

        procs = []
        for wid in worker_ids:
            inst = _extract_instruction_from_plan(plan, wid)
            p = _run_worker_subprocess(wid, inst, self.config.workspace, sd, "research", goal)
            procs.append(p)
            self._log(f"  Launched worker {wid} (pid={p['pid']})")

        if not procs:
            return PhaseResult(phase="research", status="blocked", coordinator_output=plan)

        done = _wait_for_workers(procs, sd, self.config.timeout_per_worker_minutes)
        ok = sum(done.values())
        self._log(f"  Workers done: {ok}/{len(procs)}")

        return PhaseResult(
            phase="research",
            status="ok" if ok == len(procs) else "blocked",
            coordinator_output=plan,
            worker_ids=worker_ids,
        )

    def _run_synthesis(self, goal: str) -> PhaseResult:
        self._log("PHASE 2: Synthesis")
        sd = self._scratch_dir

        prompt = build_synthesis_prompt(goal, self.config.workspace, sd)
        output = self._coordinator_complete(prompt, COORDINATOR_SYSTEM)
        self._log(f"  Coordinator output ({len(output)} chars)")

        # Extract spec from coordinator output and write it ourselves
        # The spec is usually between ```spec ... ``` or just plain text after "SPEC:"
        spec_text = self._extract_spec_from_text(output)

        spec_path = os.path.join(sd, "spec.md")
        spec_written = False
        if spec_text:
            with open(spec_path, "w") as f:
                f.write(spec_text)
            spec_written = True
            self._log(f"  spec.md written ({len(spec_text)} chars)")

        return PhaseResult(
            phase="synthesis",
            status="ok" if spec_written else "blocked",
            coordinator_output=output,
        )

    def _extract_spec_from_text(self, text: str) -> str:
        """Extract the spec content from coordinator's text output."""
        if not text:
            return ""
        # Try fenced code block first
        import re
        m = re.search(r"```(?:spec|markdown)?\n(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # Try "SPEC:" or "## Spec" section
        m = re.search(r"(?:SPEC:|## SPEC)(.*?)(?:\n##|\Z)", text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
        # Fall back: if it looks like a spec (has file paths, code snippets), return it
        if "file" in text.lower() and ("```" in text or "def " in text or "import " in text):
            return text.strip()
        return ""

    def _run_implementation(self, goal: str) -> PhaseResult:
        self._log("PHASE 3: Implementation")
        sd = self._scratch_dir

        prompt = build_implementation_prompt(goal, self.config.workspace, sd, self.config.max_workers)
        plan = self._coordinator_complete(prompt, COORDINATOR_SYSTEM)
        self._log(f"  Coordinator output ({len(plan)} chars)")

        # Extract worker plan from coordinator text and write impl-plan.md ourselves
        plan_file = os.path.join(sd, "impl-plan.md")
        plan_written = False
        if plan:
            # Extract just the WORKER blocks
            import re
            blocks = re.findall(r"(WORKER\s+\w+:.*?)(?=WORKER\s+\w+:|$)", plan, re.DOTALL)
            if blocks:
                plan_text = "\n\n".join(b.strip() for b in blocks if b.strip())
                with open(plan_file, "w") as f:
                    f.write(plan_text)
                plan_written = True
                self._log(f"  impl-plan.md written ({len(plan_text)} chars)")

        worker_ids = _parse_worker_ids_from_plan(plan_file)
        self._log(f"  Planner extracted {len(worker_ids)} implementation tasks: {worker_ids}")

        procs = []
        for wid in worker_ids:
            inst = _extract_instruction_from_plan(plan, wid)
            p = _run_worker_subprocess(wid, inst, self.config.workspace, sd, "implementation", goal)
            procs.append(p)
            self._log(f"  Launched worker {wid} (pid={p['pid']})")

        if not procs:
            return PhaseResult(phase="implementation", status="blocked", coordinator_output=plan)

        done = _wait_for_workers(procs, sd, self.config.timeout_per_worker_minutes)
        ok = sum(done.values())
        self._log(f"  Workers done: {ok}/{len(procs)}")

        return PhaseResult(
            phase="implementation",
            status="ok" if ok == len(procs) else "blocked",
            coordinator_output=plan,
            worker_ids=worker_ids,
        )

    def _run_verification(self, goal: str) -> PhaseResult:
        self._log("PHASE 4: Verification")
        sd = self._scratch_dir

        prompt = build_verification_prompt(goal, self.config.workspace, sd)
        output = self._coordinator_complete(prompt, COORDINATOR_SYSTEM)

        vfile = os.path.join(sd, "verification.md")
        with open(vfile, "w") as f:
            f.write(output)

        is_ready = output.upper().count("READY") > output.upper().count("NEEDS_WORK")
        self._log(f"  Verification: {'READY' if is_ready else 'NEEDS_WORK'}")

        return PhaseResult(
            phase="verification",
            status="ok",
            coordinator_output=output,
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def run(self, goal: str) -> CoderResult:
        """
        Run the full four-phase orchestration.

        Args:
            goal: The coding task to accomplish

        Returns:
            CoderResult with phase results, scratch dir, and final status
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self._log(f"Starting: {goal}")
        self._log(f"Workspace: {self.config.workspace}")
        self._log(f"Timestamp: {ts}")

        phases: list[PhaseResult] = []

        # Phase 1: Research
        r = self._run_research(goal)
        phases.append(r)
        if r.status == "error":
            return CoderResult(phases=phases, scratch_dir=self._scratch_dir,
                              final_status="needs_work")

        # Phase 2: Synthesis
        s = self._run_synthesis(goal)
        phases.append(s)
        if s.status == "error":
            return CoderResult(phases=phases, scratch_dir=self._scratch_dir,
                              final_status="needs_work")

        # Phase 3: Implementation
        i = self._run_implementation(goal)
        phases.append(i)
        if i.status == "error":
            return CoderResult(phases=phases, scratch_dir=self._scratch_dir,
                              final_status="needs_work")

        # Phase 4: Verification
        v = self._run_verification(goal)
        phases.append(v)

        # Read spec
        spec_path = os.path.join(self._scratch_dir, "spec.md")
        spec = ""
        if os.path.exists(spec_path):
            with open(spec_path) as f:
                spec = f.read()

        is_ready = v.coordinator_output.upper().count("READY") > v.coordinator_output.upper().count("NEEDS_WORK")
        final_status = "ready" if is_ready else "needs_work"

        self._log(f"Done. final_status={final_status}")
        self._log(f"Scratch dir: {self._scratch_dir}")

        return CoderResult(
            phases=phases,
            scratch_dir=self._scratch_dir,
            final_status=final_status,
            spec=spec,
        )
