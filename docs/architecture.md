# agentd - Architecture & Design Guide

A comprehensive guide to understanding the **agentd** framework — an LLM agent utilities library that gives any OpenAI-compatible client transparent access to MCP tools, bash execution, and sandboxed code running.

---

## What is agentd?

agentd solves a fundamental problem: **how do you give an LLM safe, discoverable access to external tools?**

It provides two distinct paradigms, both implemented as monkey-patches on the standard OpenAI Python SDK:

```mermaid
graph TB
    subgraph "Two Paradigms"
        PTC["<b>Programmatic Tool Calling</b><br/>LLM writes code in markdown fences<br/>Tools exposed as Python imports"]
        TRAD["<b>Traditional Tool Calling</b><br/>Standard JSON tool_calls<br/>MCP tools auto-injected"]
    end

    APP["Your Application"]
    SDK["OpenAI Python SDK"]

    APP --> SDK
    SDK -.->|"patch_openai_with_ptc()"| PTC
    SDK -.->|"patch_openai_with_mcp()"| TRAD

    PTC --> EXEC["Executors<br/>(subprocess, sandbox, microVM)"]
    PTC --> BRIDGE["MCP Bridge"]
    TRAD --> MCP["MCP Servers"]
    BRIDGE --> MCP

    style PTC fill:#e1f5fe,stroke:#0288d1
    style TRAD fill:#f3e5f5,stroke:#7b1fa2
    style EXEC fill:#fff3e0,stroke:#ef6c00
    style BRIDGE fill:#e8f5e9,stroke:#388e3c
```

---

## High-Level Architecture

```mermaid
graph TB
    USER["User Application"] --> CLIENT["OpenAI Client<br/>(patched)"]

    CLIENT --> COMP["chat.completions.create()"]
    CLIENT --> RESP["responses.create()"]

    COMP --> PTC_H["PTC Handler<br/><i>parse fences, execute code</i>"]
    COMP --> MCP_H["MCP Handler<br/><i>intercept tool_calls</i>"]
    RESP --> PTC_H
    RESP --> MCP_H

    PTC_H --> EXECUTOR
    PTC_H --> SKILLS["Skills Directory<br/><i>auto-generated</i>"]

    MCP_H --> MCP_EXEC["MCP Tool Execution"]
    MCP_H --> LOCAL["Local @tool Functions"]

    subgraph EXECUTOR["Executor Layer"]
        SUB["SubprocessExecutor<br/><i>default, no isolation</i>"]
        SRT["SandboxRuntimeExecutor<br/><i>OS-level sandbox</i>"]
        MSB["MicrosandboxCLIExecutor<br/><i>microVM isolation</i>"]
    end

    SKILLS --> BRIDGE_S["MCP Bridge<br/><i>HTTP / Unix socket</i>"]
    BRIDGE_S --> MCP_S["MCP Servers<br/><i>stdio-based</i>"]

    MCP_EXEC --> MCP_S

    subgraph DAEMON["Agent Daemon (CLI)"]
        YAML["YAML Config"] --> AGENT["Agent"]
        AGENT --> SUB_MCP["MCP Subscriptions"]
        SUB_MCP --> NOTIFY["Notification Loop"]
        NOTIFY --> AGENT
        REPL["Interactive REPL"] --> AGENT
    end

    DAEMON -.->|"uses"| MCP_H

    style PTC_H fill:#e1f5fe,stroke:#0288d1
    style MCP_H fill:#f3e5f5,stroke:#7b1fa2
    style EXECUTOR fill:#fff3e0,stroke:#ef6c00
    style DAEMON fill:#fce4ec,stroke:#c62828
```

---

## Paradigm 1: Programmatic Tool Calling (PTC)

PTC replaces JSON `tool_calls` with **code fences** in the LLM's response. The LLM writes bash or Python directly, and agentd intercepts the stream, executes the code, and feeds results back.

### How PTC Works

```mermaid
sequenceDiagram
    participant App as Application
    participant Client as Patched Client
    participant LLM as LLM Provider
    participant Parser as Fence Parser
    participant Exec as Executor
    participant Bridge as MCP Bridge
    participant MCP as MCP Server

    App->>Client: responses.create(stream=True)

    Note over Client: Inject PTC guidance<br/>+ tool manifest into<br/>system prompt

    Client->>LLM: Request (with guidance)

    loop Streaming Response
        LLM-->>Client: Text delta: "Let me check..."
        Client-->>App: TextDelta event

        LLM-->>Client: "```bash:execute\nls -la\n```"
        Client->>Parser: Parse code fence
        Parser-->>Client: CodeFence(bash, execute, "ls -la")
        Client->>Exec: execute_bash("ls -la")
        Exec-->>Client: (output, exit_code)
        Client-->>App: CodeExecution event

        LLM-->>Client: "```python:execute\nfrom lib.tools import read_file\n```"
        Client->>Parser: Parse code fence
        Parser-->>Client: CodeFence(python, execute, ...)
        Client->>Exec: execute_python(code)

        Note over Exec,Bridge: Python calls lib.tools<br/>which POSTs to MCP Bridge

        Exec->>Bridge: POST /call/read_file
        Bridge->>MCP: call_tool("read_file", ...)
        MCP-->>Bridge: result
        Bridge-->>Exec: response
        Exec-->>Client: (output, exit_code)
        Client-->>App: CodeExecution event
    end

    Client->>LLM: Feed execution results back
    Note over Client,LLM: Loop up to MAX_LOOPS (20)

    Client-->>App: TurnEnd event
```

### Code Fence Format

The LLM produces fenced code blocks with a `type:action` header:

```mermaid
graph LR
    subgraph "Fence Syntax"
        F1["\```bash:execute<br/>ls -la<br/>\```"]
        F2["\```python:execute<br/>from lib.tools import calc<br/>print(calc('2+2'))<br/>\```"]
        F3["\```app.py:create<br/>print('Hello!')<br/>\```"]
    end

    F1 -->|"action: execute"| E1["Run in shell"]
    F2 -->|"action: execute"| E2["Run as Python"]
    F3 -->|"action: create"| E3["Write to file"]

    style F1 fill:#e8f5e9,stroke:#388e3c
    style F2 fill:#e1f5fe,stroke:#0288d1
    style F3 fill:#fff3e0,stroke:#ef6c00
```

**Execution rules:**

- Back-to-back fences (no text between) run **in parallel**
- Text between fences forces **sequential** execution
- Also supports Claude's XML `<invoke>` format

### Skills Directory (Auto-Generated)

When MCP servers or `@tool` functions are provided, PTC generates a skills directory that the LLM can explore:

```mermaid
graph TB
    subgraph Sources
        MCP_T["MCP Server Tools<br/><i>e.g. filesystem, fetch</i>"]
        LOCAL_T["@tool Functions<br/><i>e.g. calculate</i>"]
    end

    GEN["Skills Generator<br/><i>(at patch time)</i>"]

    MCP_T --> GEN
    LOCAL_T --> GEN

    subgraph "skills/ directory"
        CLI["cli.py<br/><i>skills list|read|run</i>"]
        LIB["lib/tools.py<br/><i>Python bindings for ALL tools</i>"]

        subgraph "filesystem/"
            SM1["SKILL.md<br/><i>YAML frontmatter + docs</i>"]
            SC1["scripts/<br/><i>example scripts</i>"]
        end

        subgraph "local/"
            SM2["SKILL.md"]
            SC2["scripts/"]
        end
    end

    GEN --> CLI
    GEN --> LIB
    GEN --> SM1
    GEN --> SM2

    style GEN fill:#e8eaf6,stroke:#3f51b5
    style LIB fill:#e1f5fe,stroke:#0288d1
```

The LLM discovers tools naturally:

```
skills list → filesystem, local
skills read filesystem → SKILL.md with tool docs
python -c "from lib.tools import read_file; print(read_file(path='/tmp/x'))"
```

`lib/tools.py` contains generated Python functions that POST to the MCP Bridge:

```python
def read_file(path: str) -> dict:
    return _call("read_file", path=path)  # → POST http://bridge/call/read_file
```

---

## Paradigm 2: Traditional Tool Calling (MCP Patch)

For standard JSON `tool_calls` — the patched client transparently handles MCP tool discovery, execution, and result feeding.

### How MCP Patching Works

```mermaid
sequenceDiagram
    participant App as Application
    participant Client as Patched Client
    participant LLM as LLM Provider
    participant MCP as MCP Server(s)

    App->>Client: completions.create(<br/>mcp_servers=[fs_server])

    Client->>MCP: Connect + list tools
    MCP-->>Client: Tool schemas

    Note over Client: Merge MCP schemas +<br/>@tool schemas into<br/>tools parameter

    Client->>LLM: Request (with tool schemas)
    LLM-->>Client: tool_call: read_file({path: "/tmp/x"})

    alt MCP Tool
        Client->>MCP: call_tool("read_file", {path: "/tmp/x"})
        MCP-->>Client: result
    else @tool Function
        Client->>Client: Execute local function
    end

    Client->>LLM: Feed tool result back
    LLM-->>Client: "The file contains..."

    Note over Client,LLM: Loop until no tool_calls<br/>(max 20 iterations)

    Client-->>App: Final response
```

### Multi-Provider Support via LiteLLM

```mermaid
graph LR
    CLIENT["Patched OpenAI Client"] --> LITELLM["LiteLLM Translation Layer"]

    LITELLM --> OAI["OpenAI<br/><i>gpt-4o</i>"]
    LITELLM --> ANT["Anthropic<br/><i>claude-sonnet-4-20250514</i>"]
    LITELLM --> GEM["Google<br/><i>gemini/gemini-2.0-flash</i>"]
    LITELLM --> OTHER["Other providers..."]

    style LITELLM fill:#e8eaf6,stroke:#3f51b5
```

The patching is transparent — you use the standard OpenAI SDK interface regardless of the underlying provider.

---

## Executor Layer

The Executor protocol defines how code is actually run. All executors implement the same interface:

```mermaid
graph TB
    PROTO["Executor Protocol<br/><i>execute_bash() / execute_python() / create_file()</i>"]

    PROTO --> SUB
    PROTO --> SRT
    PROTO --> MSB

    subgraph SUB["SubprocessExecutor"]
        direction TB
        S1["Persistent bash shell session"]
        S2["Unique markers for output detection"]
        S3["No isolation"]
        S4["Default choice"]
    end

    subgraph SRT["SandboxRuntimeExecutor"]
        direction TB
        R1["OS-level primitives<br/><i>sandbox-exec (macOS)<br/>bubblewrap (Linux)</i>"]
        R2["Network allow-list"]
        R3["Filesystem read/write restrictions"]
        R4["Unix socket for MCP Bridge"]
    end

    subgraph MSB["MicrosandboxCLIExecutor"]
        direction TB
        M1["Hardware-isolated microVMs"]
        M2["Volume mounting for workspace"]
        M3["Requires KVM (Linux)<br/>or Apple Silicon (macOS)"]
        M4["Maximum isolation"]
    end

    subgraph SHARED["Shared Features"]
        SNAP["Snapshot Manager<br/><i>save/restore workspace state</i>"]
    end

    SRT --> SHARED
    MSB --> SHARED

    style PROTO fill:#e8eaf6,stroke:#3f51b5
    style SUB fill:#e8f5e9,stroke:#388e3c
    style SRT fill:#fff3e0,stroke:#ef6c00
    style MSB fill:#fce4ec,stroke:#c62828
```

### Isolation Comparison

```mermaid
graph LR
    subgraph "No Isolation"
        A["SubprocessExecutor<br/><i>Development / trusted code</i>"]
    end

    subgraph "OS-Level"
        B["SandboxRuntimeExecutor<br/><i>Lightweight, fast startup</i><br/><i>Network + filesystem restrictions</i>"]
    end

    subgraph "Hardware Isolation"
        C["MicrosandboxCLIExecutor<br/><i>MicroVM, full isolation</i><br/><i>Snapshots for time travel</i>"]
    end

    A -->|"More isolation"| B -->|"Maximum isolation"| C

    style A fill:#c8e6c9
    style B fill:#ffe0b2
    style C fill:#ffcdd2
```

---

## MCP Bridge

The MCP Bridge is an HTTP/Unix socket server that sits between sandboxed code and MCP servers on the host. It's essential for PTC because the LLM's generated Python code runs in a subprocess (possibly sandboxed) and needs a way to call MCP tools.

```mermaid
graph TB
    subgraph "Sandboxed Process"
        CODE["Generated Python Code"]
        TOOLS["lib/tools.py<br/><i>_call() function</i>"]
        CODE --> TOOLS
    end

    TOOLS -->|"POST /call/read_file<br/>(HTTP or Unix socket)"| BRIDGE

    subgraph BRIDGE["MCP Bridge Server"]
        ROUTER["Request Router"]
        ROUTER -->|"MCP tool"| MCP_D["MCP Dispatch"]
        ROUTER -->|"@tool function"| LOCAL_D["Local Dispatch"]
    end

    subgraph "Host Environment"
        MCP_D --> MCP1["MCP Server 1<br/><i>filesystem</i>"]
        MCP_D --> MCP2["MCP Server 2<br/><i>fetch</i>"]
        LOCAL_D --> FUNC["@tool functions<br/><i>in-process</i>"]
    end

    HEALTH["GET /health"] --> BRIDGE
    LIST["GET /tools"] --> BRIDGE

    style BRIDGE fill:#e8f5e9,stroke:#388e3c
    style CODE fill:#e1f5fe,stroke:#0288d1
```

**Endpoints:**

- `POST /call/{tool_name}` — Execute a tool by name
- `GET /tools` — List available tools and schemas
- `GET /health` — Health check

**Transport modes:**

- **TCP** (port-based) — For unsandboxed or network-accessible environments
- **Unix socket** — For sandboxed execution where network is isolated but filesystem is shared

---

## Agent Daemon

The Agent Daemon (`agentd` CLI) runs YAML-configured agents that **react to MCP resource changes** using the subscription pattern.

```mermaid
graph TB
    YAML["config.yaml"] -->|"agentd config.yaml"| LOADER["Config Loader"]
    LOADER --> A1["Agent 1"]
    LOADER --> A2["Agent 2"]
    LOADER --> AN["Agent N..."]

    subgraph AGENT["Agent Lifecycle"]
        direction TB
        CONNECT["Connect to MCP Servers"]
        SUBSCRIBE["Subscribe to Resources<br/><i>tool://fetch/?url=...</i>"]

        CONNECT --> SUBSCRIBE

        SUBSCRIBE --> LOOP

        subgraph LOOP["Concurrent Event Loops"]
            NOTIFY_LOOP["Notification Processor<br/><i>MCP resource changes</i>"]
            USER_LOOP["User REPL<br/><i>Interactive input</i>"]
        end

        NOTIFY_LOOP -->|"resource changed"| CALL_TOOL["Call Tool via URI"]
        CALL_TOOL --> LLM_CALL["Send to LLM<br/><i>(with MCP tools available)</i>"]
        LLM_CALL --> RESPONSE["Process Response"]

        USER_LOOP -->|"user prompt"| LLM_CALL
    end

    A1 --> AGENT

    style YAML fill:#fff3e0,stroke:#ef6c00
    style LOOP fill:#e8eaf6,stroke:#3f51b5
```

### Subscription Flow

```mermaid
sequenceDiagram
    participant Daemon as Agent Daemon
    participant MCP_Sub as MCP Subscribe Server
    participant MCP_Tool as MCP Tool Server
    participant LLM as LLM

    Daemon->>MCP_Sub: Connect
    Daemon->>MCP_Tool: Connect
    Daemon->>MCP_Sub: subscribe("tool://fetch/?url=https://example.com")

    Note over MCP_Sub: Polls URL at configured interval

    MCP_Sub-->>Daemon: Notification: resource changed
    Daemon->>MCP_Sub: call_tool("fetch", {url: "..."})
    MCP_Sub-->>Daemon: New content

    Daemon->>LLM: "Tool tool://fetch/... returned: [content]"
    LLM-->>Daemon: "I'll save this. [tool_call: write_file]"
    Daemon->>MCP_Tool: call_tool("write_file", {...})
    MCP_Tool-->>Daemon: Success
    Daemon->>LLM: Tool result
    LLM-->>Daemon: "Saved to ./output/data.txt"
```

---

## Conversation Logging

All interactions are logged as JSONL files for debugging and analysis:

```mermaid
graph LR
    subgraph "Events Logged"
        E1["session_start<br/><i>patch type, model</i>"]
        E2["message<br/><i>role, content</i>"]
        E3["tool_call<br/><i>type, name, input</i>"]
        E4["tool_result<br/><i>name, output</i>"]
    end

    E1 --> LOG["ConversationLog<br/><i>Thread-safe JSONL writer</i>"]
    E2 --> LOG
    E3 --> LOG
    E4 --> LOG

    LOG --> FILE["logs/{session_id}.jsonl"]

    ENV1["AGENTD_LOG_DISABLE=1"] -.->|"disables"| LOG
    ENV2["AGENTD_LOG_DIR=path"] -.->|"configures"| FILE

    style LOG fill:#e8eaf6,stroke:#3f51b5
```

---

## @tool Decorator

Register Python functions as tools with auto-generated JSON schema:

```mermaid
graph LR
    FUNC["@tool<br/>def calculate(expression: str) -> str:<br/>&nbsp;&nbsp;'''Evaluate math.'''<br/>&nbsp;&nbsp;..."]

    FUNC --> INSPECT["Inspect signature<br/>+ type hints<br/>+ docstring"]

    INSPECT --> SCHEMA["SCHEMA_REGISTRY<br/><i>JSON Schema for LLM</i>"]
    INSPECT --> REGISTRY["FUNCTION_REGISTRY<br/><i>Callable reference</i>"]

    SCHEMA --> PTC_USE["PTC: included in<br/>skills/lib/tools.py"]
    SCHEMA --> MCP_USE["MCP Patch: merged<br/>into tools parameter"]

    REGISTRY --> PTC_EXEC["PTC: called via<br/>MCP Bridge"]
    REGISTRY --> MCP_EXEC["MCP Patch: called<br/>directly in-process"]

    style FUNC fill:#e1f5fe,stroke:#0288d1
```

---

## Data Flow Summary

```mermaid
graph TB
    subgraph "Entry Points"
        API_PTC["patch_openai_with_ptc()"]
        API_MCP["patch_openai_with_mcp()"]
        API_DAEMON["agentd CLI"]
    end

    subgraph "Core Modules"
        PTC["ptc.py<br/><i>~2700 lines</i><br/>Fence parsing, streaming,<br/>executor management"]
        PATCH["patch.py<br/><i>~1800 lines</i><br/>SDK monkey-patching,<br/>tool_calls loop"]
        APP["app.py<br/><i>Agent daemon,<br/>YAML config, async loops</i>"]
    end

    subgraph "Supporting Modules"
        BRIDGE["mcp_bridge.py<br/><i>HTTP/socket proxy</i>"]
        TOOL_DEC["tool_decorator.py<br/><i>@tool + registries</i>"]
        LOGGER["conversation_logger.py<br/><i>JSONL logging</i>"]
        CONFIG["model/config.py<br/><i>YAML dataclasses</i>"]
    end

    subgraph "Executors"
        SUB_E["SubprocessExecutor"]
        SRT_E["SandboxRuntimeExecutor"]
        MSB_E["MicrosandboxCLIExecutor"]
    end

    API_PTC --> PTC
    API_MCP --> PATCH
    API_DAEMON --> APP

    PTC --> BRIDGE
    PTC --> TOOL_DEC
    PTC --> LOGGER
    PTC --> SUB_E & SRT_E & MSB_E

    PATCH --> TOOL_DEC
    PATCH --> LOGGER

    APP --> PATCH
    APP --> CONFIG

    style PTC fill:#e1f5fe,stroke:#0288d1
    style PATCH fill:#f3e5f5,stroke:#7b1fa2
    style APP fill:#fce4ec,stroke:#c62828
```

---

## Key Design Decisions

| Decision                            | Rationale                                                               |
|-------------------------------------|-------------------------------------------------------------------------|
| **Monkey-patching OpenAI SDK**      | Zero API changes — existing code works with one line added              |
| **Code fences over tool_calls**     | More natural for bash/Python; LLM can chain logic in a single block     |
| **Auto-generated skills directory** | LLM discovers tools via filesystem exploration, not system prompt bloat |
| **Pluggable executors**             | Same interface from dev (subprocess) through production (microVM)       |
| **MCP Bridge as HTTP server**       | Sandboxed processes can't access host MCP connections directly          |
| **Unix socket transport**           | Works inside network-isolated sandboxes where TCP is blocked            |
| **LiteLLM for multi-provider**      | One patched client works with OpenAI, Anthropic, Google, etc.           |
| **MAX_LOOPS = 20**                  | Prevents runaway tool-calling loops                                     |
| **Output truncation at 20k chars**  | Prevents token waste on huge command outputs                            |

---

## Quick Reference

### Installation

```bash
pip install agentd
```

### PTC (code fences)

```python
from agentd import patch_openai_with_ptc, display_events
client = patch_openai_with_ptc(OpenAI(), cwd="./workspace")
stream = client.responses.create(model="...", input="...", stream=True)
for event in display_events(stream):
    ...
```

### MCP Patch (tool_calls)

```python
from agentd import patch_openai_with_mcp
client = patch_openai_with_mcp(OpenAI())
response = client.chat.completions.create(model="...", messages=[...], mcp_servers=[...])
```

### Agent Daemon

```bash
agentd config.yaml
```

### Sandboxed Execution

```python
from agentd import create_sandbox_runtime_executor, create_microsandbox_cli_executor

# OS-level (lightweight)
executor = create_sandbox_runtime_executor(conversation_id="s1", allowed_domains=["github.com"])

# MicroVM (maximum isolation)
executor = create_microsandbox_cli_executor(conversation_id="s1", image="python")

client = patch_openai_with_ptc(OpenAI(), executor=executor)
```
