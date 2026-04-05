# ContextGarden

A standalone MCP (Model Context Protocol) server that turns any directory of source code or markdown notes into a searchable knowledge graph. Connect it to your LLM client and call `retrieve_context` before answering any project-specific question.

Ships empty — workspaces are registered at runtime via MCP tools.

---

## How it works

1. You register a source directory as a **workspace**.
2. ContextGarden mirrors the source into structured markdown notes (`md_db/`).
3. A file watcher keeps notes in sync as code changes.
4. On query, a hybrid RAG pipeline retrieves relevant notes and synthesizes a context summary.
5. Your LLM client receives the formatted context and answers with it.

---

## Prerequisites

- **Node.js** 18+
- **Conda** (for the Python knowledge graph backend)
- An LLM provider (Ollama, OpenAI, or Anthropic)
- An embedding provider (Ollama local or OpenAI)

---

## Installation

```bash
# 1. Install Node dependencies
npm install

# 2. Create the Python environment
conda env create -f graph/environment.yml

# 3. (Optional) Verify the Python RPC server starts
conda run -n contextgarden python -m graph
```

---

## Provider setup

### Ollama (default — fully local)

The default configuration points to a local Ollama instance. No API keys needed.

```bash
# Install Ollama: https://ollama.com
ollama pull nomic-embed-text   # embeddings
ollama pull cogito:8b          # synthesis LLM (swap for any model you prefer)
```

Start the server with no extra config:

```bash
npx tsx bin/context-garden.ts --data-dir /path/to/your/data
```

---

### OpenAI

Set environment variables before starting:

```bash
export OPENAI_API_KEY=sk-...
export CG_EMBED_PROVIDER=openai
export CG_EMBED_MODEL=text-embedding-3-small
export CG_LLM_PROVIDER=openai
export CG_LLM_MODEL=gpt-4o
```

Or write them to `.context-garden/config.json` in your data directory (see [Config file](#config-file)).

---

### Anthropic

Anthropic is supported for the **synthesis LLM only** (not embeddings — use Ollama or OpenAI for those).

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export CG_LLM_PROVIDER=anthropic
export CG_LLM_MODEL=claude-sonnet-4-6

# Still need an embed provider
export OPENAI_API_KEY=sk-...
export CG_EMBED_PROVIDER=openai
export CG_EMBED_MODEL=text-embedding-3-small
```

---

## Starting the server

```bash
# Default data directory = current working directory
npx tsx bin/context-garden.ts

# Explicit data directory
npx tsx bin/context-garden.ts --data-dir /path/to/data

# Via environment variable
CG_DATA_DIR=/path/to/data npx tsx bin/context-garden.ts
```

The server communicates over **stdio** (MCP standard). Connect it via your MCP client config.

---

## MCP client configuration

### Claude Code / Claude Desktop

Add to your MCP config (`claude_desktop_config.json` or `.claude/mcp.json`):

```json
{
  "mcpServers": {
    "context-garden": {
      "command": "npx",
      "args": ["tsx", "/absolute/path/to/ContextGarden/bin/context-garden.ts", "--data-dir", "/path/to/data"],
      "env": {
        "CG_EMBED_PROVIDER": "ollama",
        "CG_LLM_PROVIDER": "ollama"
      }
    }
  }
}
```

For OpenAI:

```json
{
  "mcpServers": {
    "context-garden": {
      "command": "npx",
      "args": ["tsx", "/absolute/path/to/ContextGarden/bin/context-garden.ts", "--data-dir", "/path/to/data"],
      "env": {
        "OPENAI_API_KEY": "sk-...",
        "CG_EMBED_PROVIDER": "openai",
        "CG_EMBED_MODEL": "text-embedding-3-small",
        "CG_LLM_PROVIDER": "openai",
        "CG_LLM_MODEL": "gpt-4o"
      }
    }
  }
}
```

For Anthropic synthesis + OpenAI embeddings:

```json
{
  "mcpServers": {
    "context-garden": {
      "command": "npx",
      "args": ["tsx", "/absolute/path/to/ContextGarden/bin/context-garden.ts", "--data-dir", "/path/to/data"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "OPENAI_API_KEY": "sk-...",
        "CG_EMBED_PROVIDER": "openai",
        "CG_EMBED_MODEL": "text-embedding-3-small",
        "CG_LLM_PROVIDER": "anthropic",
        "CG_LLM_MODEL": "claude-sonnet-4-6"
      }
    }
  }
}
```

---

## Config file

You can persist configuration to `<data-dir>/.context-garden/config.json`:

```json
{
  "embedding": {
    "provider": "openai",
    "model": "text-embedding-3-small",
    "host": "https://api.openai.com",
    "apiKey": "sk-..."
  },
  "synthesizer": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "host": "https://api.anthropic.com",
    "apiKey": "sk-ant-..."
  }
}
```

Config cascade (last wins): **defaults → config.json → env vars → `configure` tool at runtime**

---

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `CG_DATA_DIR` | Data directory override | `cwd` |
| `CG_EMBED_PROVIDER` | Embedding provider: `ollama`, `openai`, `local` | `ollama` |
| `CG_EMBED_MODEL` | Embedding model name | `nomic-embed-text:latest` |
| `CG_EMBED_HOST` | Embedding provider host URL | `http://localhost:11434` |
| `CG_EMBED_API_KEY` | Embedding API key | — |
| `CG_LLM_PROVIDER` | LLM provider: `ollama`, `openai`, `anthropic` | `ollama` |
| `CG_LLM_MODEL` | LLM model name | `cogito:8b` |
| `CG_LLM_HOST` | LLM provider host URL | `http://localhost:11434` |
| `CG_LLM_API_KEY` | LLM API key | — |
| `OPENAI_API_KEY` | OpenAI key (fallback for embed + LLM) | — |
| `ANTHROPIC_API_KEY` | Anthropic key (fallback for LLM) | — |

---

## MCP tools

Once connected, your LLM client has access to 7 tools:

| Tool | Description |
|---|---|
| `retrieve_context` | Hybrid RAG query — returns markdown-formatted context from the knowledge graph |
| `find_path` | Shortest path between two concepts in the knowledge graph |
| `rate_context` | Rate a retrieval result to improve future queries |
| `register_workspace` | Register a source directory for mirroring + indexing |
| `list_workspaces` | List all registered workspaces and their status |
| `unregister_workspace` | Stop watcher, delete mirrored notes, remove workspace |
| `configure` | View or update embed/LLM config at runtime; `persist=true` saves to config.json |

### Registering a workspace

```
register_workspace(
  name: "my-project",
  source_dir: "/absolute/path/to/my-project",
  languages: ["typescript"]   // or: "python", "c"
)
```

Supported languages: `typescript`, `python`, `c`

After registration, mirrored notes appear in `<data-dir>/md_db/code/my-project/`.

### Querying context

```
retrieve_context(
  query: "how does authentication work?",
  workspace: "my-project",   // optional — omit to search all workspaces
  max_chars: 15000           // optional
)
```

### Runtime reconfiguration

```
configure(
  llm_provider: "anthropic",
  llm_model: "claude-opus-4-6",
  persist: true
)
```

---

## Data directory layout

```
<data-dir>/
  md_db/
    code/
      <workspace-name>/     ← mirrored notes per workspace
  .context-garden/
    config.json             ← persisted config (optional)
    workspaces.json         ← workspace registry
    knowledge_graph/        ← LlamaIndex vector + graph index
```

---

## Troubleshooting

**Python subprocess fails to start**
- Verify the conda env exists: `conda env list | grep contextgarden`
- Recreate if missing: `conda env create -f graph/environment.yml`
- Test manually: `conda run -n contextgarden python -m graph`

**No results from `retrieve_context`**
- Check that a workspace is registered: call `list_workspaces`
- Indexing runs in the background after `register_workspace` — wait a moment for large codebases
- Verify Ollama is running (default provider): `curl http://localhost:11434/api/tags`

**Embedding model not found (Ollama)**
- Pull the model: `ollama pull nomic-embed-text`

**Wrong model for synthesis**
- Use `configure(llm_model: "...", persist: true)` or set `CG_LLM_MODEL` env var
