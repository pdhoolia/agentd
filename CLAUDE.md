# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

agentd is an LLM agent utilities framework with two main paradigms for tool-enabled agents:

- **Programmatic Tool Calling (PTC)**: LLM writes bash/python in markdown code fences instead of JSON tool_calls
- **Traditional Tool Calling**: Monkey-patches OpenAI SDK to transparently handle MCP tools in the standard tool_calls loop

Also includes an **Agent Daemon** (CLI `agentd`) for YAML-configured reactive agents using MCP resource subscriptions.

## Build & Test

```bash
# Install (uses uv / hatchling)
pip install -e .
# or
uv pip install -e .

# Run all tests
pytest test/

# Run a single test file
pytest test/test_ptc.py

# Run a specific test
pytest test/test_ptc.py::TestCodeFenceParsing::test_parse_basic_execute
```

No CI configured. No linter config — no formal lint step.

## Architecture

### Core Modules (agentd/)

- **ptc.py** (~2700 lines) — PTC engine. Parses markdown code fences (```bash:execute```), streams events (TextDelta, CodeExecution, TurnEnd), manages executor lifecycle. Key: `display_events()`, `parse_code_fences()`, `CodeFence` dataclass, `_StreamBuffer` for incremental fence parsing.

- **patch.py** (~1800 lines) — Monkey-patches `OpenAI().chat.completions.create` and `OpenAI().responses.create` (sync/async) to intercept MCP tool calls. Supports multi-provider via LiteLLM. Key: `patch_openai_with_mcp()`, `patch_openai_with_ptc()`.

- **mcp_bridge.py** — Local HTTP or Unix socket server that proxies tool calls from sandboxed processes back to host MCP servers. Used by sandbox executors.

- **tool_decorator.py** — `@tool` decorator to register Python functions with auto-generated JSON schema from signature + docstring.

- **app.py** — Agent daemon CLI entry point. Parses YAML config, manages async MCP subscriptions and notification loops.

- **model/config.py** — Dataclasses for agent daemon YAML configuration.

- **conversation_logger.py** — Thread-safe JSONL logging. Controlled by `AGENTD_LOG_DISABLE` and `AGENTD_LOG_DIR` env vars.

### Executor Implementations

Pluggable via the `Executor` protocol:

1. **SubprocessExecutor** (default) — `subprocess.run`, no isolation
2. **SandboxRuntimeExecutor** (`sandbox_runtime_executor.py`) — OS-level sandbox (macOS sandbox-exec / Linux bubblewrap)
3. **MicrosandboxCLIExecutor** (`microsandbox_cli_executor.py`) — MicroVM isolation via CLI
4. **MicrosandboxExecutor** (`microsandbox_executor.py`) — MicroVM via JSON-RPC API

### Key Design Patterns

- **Code fence parsing**: PTC intercepts streaming text, detects `` ```lang:action `` fences, executes them, feeds output back as assistant context. Back-to-back fences run in parallel; text between fences forces sequential execution.
- **Skills auto-generation**: MCP tools + @tool functions are combined into a skills directory with `SKILL.md` metadata files and Python bindings at `skills/lib/tools.py`. LLM discovers tools via `skills list` / `skills read <skill>`.
- **Output truncation**: Large outputs capped at `MAX_OUTPUT_TOKENS` (20k chars).
- **Tool loop limit**: `MAX_LOOPS = 20` prevents infinite execution loops in both PTC and MCP patch modes.
- **Async throughout**: Agent daemon, MCP connections, and bridge all use asyncio.

## Environment Variables

- `AGENTD_LOG_DISABLE=1` — Disable conversation logging
- `AGENTD_LOG_DIR` — Custom log directory
- `MSB_API_KEY` — Microsandbox API key

## Project Layout

- `agentd/` — Core package
- `test/` — pytest test suite
- `examples/` — Runnable examples for PTC and MCP integration
- `config/` — YAML templates for agent daemon
- `scripts/` — Setup helpers (e.g., microsandbox installation)
