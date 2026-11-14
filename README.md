# 📖 CtrlAsistent MCP Server

## 🏠 Overview
This is the CtrlAsistent MCP (Model Context Protocol) server, which works with any MCP client:
- llama.cpp
- OpenCode
- DSH Harness
- Pi Harness
- Claude Desktop
- and others

## 📁 Project Structure

```
ctrlasistent_mcp/
├── ctrlasistent_mcp.py           # Main server
├── tools_server.py               # Automatic tool discovery from tools/
├── skills_server.py              # Exposes skills (list_skills / read_skill)
├── ctrlasistent_mcp.json         # Config (mcpServers format)
├── .env                          # Sensitive data
├── core/
│   ├── CtrlAsistent_tools.py     # Tool base class
│   └── CtrlAsistent_DocumentIndexer.py   # RAG (FAISS + BM25)
├── tools/                        # Tools
│   ├── tool_weather.py
│   ├── tool_websearch.py
│   ├── tool_rag.py
│   ├── tool_xmpp.py
│   ├── tool_matrix.py
│   ├── tool_caldav.py
│   ├── tool_memory_encrypted.py
│   ├── tool_image.py
│   └── ...
├── skills/                       # SKILL.md Mappings
│   └── pdf/
│       ├── SKILL.md
│       └── scripts/
├── documents/                    # Documents for RAG indexing
│   └── __index__/                # RAG index (do not modify!)
└── memory/                       # Encrypted memory
```

## 🔧 Installation

### 1. Prerequisites
- Python **3.13**
- Virtual environment (recommended: `ctlasistent_mpc/venv`)
- **Linux**: ```python3 -m venv venv; source venv/bin/activate```
- **Windows**: ```py -m venv venv;  venv\Scripts\activate```


### 2. Dependencies
```bash
pip install -r requirements
pip install -r requirements-extra
```

The first run requires internet access to download the embedding model (~90 MB):
```bash
HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 \
  /ctlasistent_mpc/venv/bin/python -c \
  "from sentence_transformers import SentenceTransformer; \
   SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```
✅ After this, all calls run offline (`HF_HUB_OFFLINE=1` is already set in `DocumentIndexer`).

### 4. Automatic Tool Registration
If a tool is not installed, it will not be registered — the server still runs, just without that specific tool.

## ⚙️ Configuration

### `.env` — Sensitive Data and Tokens
Create `/ctlasistent_mpc/.env`:
```bash
# XMPP
XMPP_ADDRESS=user@example.com
XMPP_PASSWORD=secret

# Matrix
MATRIX_HOMESERVER=https://matrix.org
MATRIX_USER_ID=@user:matrix.org
MATRIX_PASSWORD=secret
MATRIX_RECOVERY_KEY=...

# CalDAV
CALDAV_BASE=http://127.0.0.1:5232
CALDAV_USER=user_name

# RAG (optional - default is ./documents and ./documents/__index__)
RAG_DOCS_DIR=/path/to/documents
RAG_INDEX_DIR=/path/to/documents/__index__
```
Secure the file:
```bash
chmod 600 /ctlasistent_mpc/.env
echo ".env" >> /ctlasistent_mpc/.gitignore
```
⚠️ `.env` is loaded upon server startup (`os.environ.setdefault`).


### `tools_server.py` — Disabling Tools
```python
python

# ─── DISABLE TOOLS HERE ──────────────────────────────────────
DISABLED = set()               # All tools active
# or selective:
DISABLED = {
    # "rag_index",
    # "rag_status",
    # "rag_search",
    # "ddg_search",
    # "write_file",
    # "cmd",
    # "kdeconnect",
    # "matrix_chat",
    # "xmpp_omemo",
    # "memory",
    # "memory_encrypted",
    # "caldav",
    # "image",
    # "weather",
}
```
Any tool name in `DISABLED` will not be registered — the server runs without it.

### `skills_server.py` — Disabling Skill Tools
```python
python

DISABLED = set()               # All skills tools active
# or:
DISABLED = {"list_skills", "read_skill"}
```
**When to disable:**
*   `llama.cpp` (without native skills) → `DISABLED = set()` — uses our tools
*   `DSH`, `Claude Desktop` (have native skills) → `DISABLED = {"list_skills", "read_skill"}` — prevents duplicates
If in doubt, keep it enabled — the model will choose itself.

## ✅ Verification Before Connecting
Run the server manually to see if tools are being registered:
```bash
bash

cd /ctlasistent_mpc
./venv/bin/python ctrlasistent_mcp.py
```
**Expected output on stderr:**
```
text

✅ Registered tool: ddg_search
✅ Registered tool: weather
✅ Registered tool: rag_search
✅ Registered tool: rag_index
✅ Registered tool: rag_status
⏭️ Skipping: caldav
...
```
✅ If you see `⚠️ Error loading tool_*.py`, the tool has an import problem — see [Troubleshooting](#troubleshooting).

Press `Ctrl+C` to stop.

## 🔌 Connecting to Clients

### 1. llama.cpp (llama-server)
Two options — choose one.

#### A) llama.cpp runs the tools (server-side)
The client does not need an MCP config. `llama-server` runs the MCP server itself and executes the tools internally.

Create `llama_mcp.json`:
```json
{
  "mcpServers": {
    "ctrlasistent": {
      "command": "/path/to/ctlasistent_mpc/venv/bin/python",
      "args": ["/path/to/ctlasistent_mpcctrlasistent_mcp.py"]
    }
  }
}
```
❌ Do not add `cwd` — if the path doesn't exist, `llama-server` crashes with `ENOENT`. The server handles all paths via `__file__`.

**Run:**
```bash
bash

~/ctlasistent_mpc/llama/llama-server \
  --models-dir ~/ctlasistent_mpc/models/ \
  --port 9931 -ngl 999 -t 4 -np 1 --models-max 1 \
  --ui-mcp-proxy --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on \
  --mcp-servers-config /ctrlasistent_mcp/llama_mcp.json
```

#### B) Client runs the tools (client-side)
Use this when you want to see tool calls in the client UI or change models without restarting.
Run `llama-server` without `--mcp-servers-config`, and place the MCP config in the client (OpenCode/Hermes/...).

**OpenCode**
`~/.config/opencode/opencode.json` or `.opencode.json` in the project:
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ctrlasistent": {
      "type": "local",
      "command": [
        "/ctlasistent_mpc/venv/bin/python",
        "/ctlasistent_mpc/ctrlasistent_mcp.py"
      ],
      "enabled": true
    }
  }
}
```
Restart OpenCode → `/mcp` in TUI → `ctrlasistent` green.

**Hermes Agent**
`~/.hermes/config.yaml`:
```yaml

mcp_servers:
  ctrlasistent:
    command: "/ctlasistent_mpc/venv/bin/python"
    args: ["/ctlasistent_mpc/ctrlasistent_mcp.py"]
```
Apply without restart: in the active session type `/reload-mcp`.
**Check:** `/tools` in the session → tools under `mcp__ctrlasistent__*.`.

**DSH Harness**
`cordis.patch.yml` or corresponding config:
```yaml

- insert:
    - id: mcp-ctrlasistent
      name: '@deepseek-ai/dsh-mcp-client'
      disabled: false
      config:
        serverName: ctrlasistent
        transport: stdio
        command: /ctlasistent_mpc/venv/bin/python
        args:
          - /ctlasistent_mpc/ctrlasistent_mcp.py
        failOnStartupError: true 
        startupTimeoutMs: 120000 # give it 2 minutes or more
```

**Claude Desktop**
`~/.config/Claude/claude_desktop_config.json` (Linux) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):
```json
{
  "mcpServers": {
    "ctrlasistent": {
      "command": "/ctlasistent_mpc/venv/bin/python",
      "args": ["/ctlasistent_mpc/ctrlasistent_mcp.py"]
    }
  }
}
```
Restart Claude Desktop → hammer icon in chat.

## 📚 RAG (Local Knowledge Base)
- `documents/` is recursively scanned (including subfolders).
- Exclusions: `.git`, `__pycache__`, `node_modules`, `.venv`, `venv`, `dist`, `build`.
- Hybrid indexing: FAISS (vector) + BM25 (keyword).
- Reindexing only changed files (SHA-256 hash).

### Tools
| Tool | Description |
| :--- | :--- |
| `rag_search(query, k=3)` | Searches the index, returns excerpts with [Source File: ...] |
| `rag_index()` | Starts reindexing in the background (returns immediately) |
| `rag_status()` | Returns `idle / running / done / failed` |

✅ Place files in `documents/` (anywhere inside).
The first `rag_search` automatically indexes everything.

### Reindexing
To add new files on the fly:
```bash
rag_index → rag_status  # until 'done' is visible
# or restart the server — automatically indexes everything new
```

## 🎯 Skills

### Skill.md Format
```markdown
---
name: pdf
description: PDF manipulation toolkit. Use when the user wants to create, edit, merge, split, or extract from PDF files.
---

# PDF Skill

## Instructions

...

## Scripts

Scripts are in `scripts/` relative to this SKILL.md.
```

### How the Model Uses It
1. **First step** → `list_skills()` → sees the list.
2. **Second step** → `read_skill("pdf")` → gets content with `ENVIRONMENT` header:
```
## ENVIRONMENT
- OS: Linux ...
- Python interpreter: ./venv/bin/python
- Skill directory: /ctlasistent_mpc/skills/pdf
...
```
✅ The model knows where the scripts are and which interpreter to use.

### SkillsDirectoryProvider (native clients)
For clients with native skills support (`Claude Desktop`, newer `OpenCode`), `skills_server.py` exposes `skill://` resources via `SkillsDirectoryProvider`. No configuration needed — they register automatically.

### ➕ Adding a New Tool
Create `tools/my_tool.py`:
```python
python

from typing import Any, Dict
from core.CtrlAsistent_tools import Tool


class MyTool(Tool):
    @property
    def name(self) -> str:
        return "my_tool"

    @property
    def description(self) -> str:
        return (
            "When to use this tool. 'Use THIS when...' \n"
        )
 
    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "query": {
                "type": "string",
                "description": "What the tool accepts",
            },
        }

    def execute(self, query: str) -> str:
        return f"Result for: {query}"
```
**Rules:**
*   The class must inherit from `Tool` (it does not need `__init__.py` in `tools/`).
*   `name` must be unique (check for collisions with other MCP servers).
*   `description` must state when to use it, not just what it does.
*   `execute` can have type hints (used for schema) or a `parameters_schema` property.
*   For images, return `fastmcp.utilities.types.Image` (not a base64 string).
*   For asynchronous calls, use the `_run_safely` wrapper (see `tool_xmpp.py`).
*   Restart the client → the tool is automatically registered. No changes to `tools_server.py` or `ctrlasistent_mcp.py` are needed.

## 🐛 Troubleshooting
### Server runs, but tools are not visible in the client
1. Check manually (as above): `python ctrlasistent_mcp.py` → do you see `✅ Registered tool: ...`?
   *   **No** → import problem in the tool, see next step.
   *   **Yes** → problem is in client config.

### Client does not start the server
```bash
ps aux | grep ctrlasistent_mcp
```
*   If no processes → config is incorrect.
*   If processes exist → problem is in client registration.

### spawn ... ENOENT (Node.js clients: OpenCode, DSH)
99% of the time: `cwd` in the config points to a non-existent directory.
**Fix:** Remove `cwd` from the config.

### KeyError: 'tool_xmpp' or similar
Cause: the tool uses `sys.modules[__name__]` but the loader didn't register it.
**Fix:** already in `tools_server.py` (`sys.modules[py_file.stem] = module` before `exec_module`).

### ⚠️ Cannot instantiate X: ...
The tool cannot be instantiated. Causes:
- Constructor requires arguments (e.g., `def __init__(self, api_key)`) — add default or load from `.env`.
- Package is missing in `execute` or `__init__` — `pip install <package>`.

### ⚠️ Error loading tool_X.py: ...
Module crashes on import. Most common:
- Missing package → `pip install`.
- Syntax error → `python -c "import tools.tool_X"` for details.
- `KeyError / TypeError`: `unexpected keyword argument`.
  *   The client (or AI) sent a parameter the tool doesn't accept (e.g., `cwd`).
  *   Resolved in `safe_fn` — unrecognized parameters are silently discarded with a log:
  ```
  text

  ⚠️ weather: dropped unknown kwargs: ['cwd']
  ```
  If you see this line, the tool is working correctly. If not, check that the filter is active in `register_tool`.

### rag_index timeout in client
Client has a 60s timeout, but reindexing takes longer.
**Solution:** `background reindex` (already implemented).
`rag_index` returns `⏳ started` immediately.
`rag_status` shows progress: `starting / indexing / done / failed`.
The first run includes embedding model loading (~15-30s).

### RAG not finding files
- Are they in `documents/`? Check: `find documents/ -type f | head`
- Are supported extensions used? See the list above.
- Are they in a skipped folder? `node_modules`, `.git`, `dist`, `build` are skipped.
- Check the log: `Found N candidate files in ...` and the list below.
  *   If N=0: run `rag_index` and check the stderr logs.

### Model hallucinates parameters
The AI adds parameters that the tool doesn't accept (`cwd`, `context`, `path`).
Resolved in `safe_fn` — the filter discards extras. In the log, you can see what was discarded.
If it persists, improve the tool's `description` to explicitly state what it accepts:
```python
"Accept ONLY the 'city' parameter (string). Does not accept paths or cwd."
```
### faiss.loader: Could not load library with AVX512 support
Cosmetic message — FAISS quietly falls back to generic build. It does not affect functionality.
**To suppress:**
```python
import logging
logging.getLogger("faiss.loader").setLevel(logging.WARNING)
```

### Model loads slowly (starting takes too long)
Run `rag_setup.py` — shows exactly where it is hanging:
```bash
cd /ctlasistent_mpc;
/ctlasistent_mpc/venv/bin/python rag_setup.py
```
You will see: `cache model, loading, scanning, counting files, indexing`.

### Hermes/OpenCode do not see tools even though they work manually
1. Check `/mcp` in the client.
2. Check that the config uses the correct format:
   *   **OpenCode:** `mcp`
   *   **Hermes:** `mcp_servers`
   *   **Claude:** `mcpServers`
3. Check that there is no `cwd` with a non-existent path.
4. Restart the client (not just `/reload-mcp`) — the tool list changes.

## 🔒 Security
- `**.env**` contains passwords — `chmod 600`, do **not** commit.
- `memory/` is encrypted with **Fernet** (or **XOR fallback** if `cryptography` is not installed).
- `__index__/` contains your original documents in chunks — do **not** share if sensitive.
- The MCP server has access to the file system.

## 📌 Note
Documentation is subject to updates — check that it is compatible with the server version you are using.
