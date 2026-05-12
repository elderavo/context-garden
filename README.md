# ContextGarden

A self-hosted MCP server that turns GitLab repos into a searchable knowledge graph. Connect it to Claude Code (or any MCP client) and call `retrieve_context` before answering project-specific questions.

Push to GitLab → webhook fires → ContextGarden git-pulls, re-mirrors, and reindexes automatically.

---

## How it works

1. You register a GitLab repo as a **workspace**.
2. ContextGarden clones it and mirrors the source into structured markdown notes.
3. A webhook keeps it in sync — every push triggers a git pull + reindex.
4. On query, a hybrid RAG pipeline (vector + graph) retrieves relevant notes and synthesizes a context summary.
5. Your LLM client receives the formatted context and answers with it.

---

## Requirements

- Docker + Docker Compose
- An embedding provider:
  `openai`-compatible API at `http://10.0.132.7:8080` is the default in this repo
  (tested with `llama.cpp`), or Ollama/OpenAI
- An LLM provider: **Ollama**, OpenAI, or Anthropic
- A GitLab instance (self-hosted or gitlab.com) with webhook support

---

## Quick start

### 1. Clone and configure

```bash
git clone <this-repo>
cd context_garden
cp docker-compose.yml docker-compose.override.yml  # edit this, not the original
```

Edit `docker-compose.override.yml` — at minimum, set your SSH key path:

```yaml
services:
  context-garden:
    volumes:
      - ./data:/data
      - /path/to/your/id_ed25519:/run/secrets/git_ssh_key:ro
      - /path/to/your/known_hosts:/root/.ssh/known_hosts:ro
    environment:
      CG_GIT_SSH_COMMAND: "ssh -F /dev/null -i /run/secrets/git_ssh_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/.ssh/known_hosts"
```

If your GitLab runs on a non-standard SSH port (e.g. 2222), add `-p 2222` to the SSH command.

### 2. Pull and start

```bash
sudo docker compose pull context-garden
sudo docker compose up -d context-garden
```

The web UI is at **http://localhost:7433**.

To pin a specific published version instead of `latest`, set `CONTEXT_GARDEN_TAG`
in your shell or `.env` before starting:

```bash
export CONTEXT_GARDEN_TAG=0.1.0
sudo docker compose pull context-garden
sudo docker compose up -d context-garden
```

To confirm the running image picked up the new version:

```bash
sudo docker compose logs -f context-garden
```

If you are developing locally and want to build from source instead of pulling GHCR,
override the service with `build: .` in `docker-compose.override.yml`.

### 3. Configure providers

Open the web UI → **Settings** tab. Set your embedding and LLM providers.

**llama.cpp embeddings at `10.0.132.7:8080` + OpenAI (LLM)** — recommended:
- Embed provider: `openai`, model: `nomic`, host: `http://10.0.132.7:8080`
- LLM provider: `openai`, model: `gpt-4o-mini`, API key: `sk-...`

**Fully local (Ollama for both)**:
- Embed: `ollama` / `nomic-embed-text:latest` / `http://host.docker.internal:11434`
- LLM: `ollama` / `cogito:8b` (or any chat model) / `http://host.docker.internal:11434`

Settings persist in the Docker volume — they survive container rebuilds.

Pull Ollama models before first use:
```bash
ollama pull nomic-embed-text
ollama pull cogito:8b   # or your preferred model
```

### 4. Connect Claude Code

```bash
claude mcp add --transport http context-garden http://localhost:7433/mcp
```

Verify:
```bash
claude mcp list
```

---

## Registering a workspace

Via MCP (in Claude Code):
```
register_workspace(
  name: "my-project",
  gitlab_url: "git@gitlab.example.com:group/my-project.git",
  branch: "master",
  languages: ["ts", "py"]   # ts, py, or c
)
```

Or via the web UI → **Workspaces** tab → Register.

Supported languages: `ts` (TypeScript/JavaScript), `py` (Python), `c` (C/C++)

After registration, ContextGarden clones the repo, mirrors it into notes, and indexes everything. Check progress in the **Jobs** tab.

---

## Webhook setup (auto-sync on push)

In your GitLab repo → Settings → Webhooks:

- **URL**: `http://<your-host>:7433/webhooks/gitlab/<workspace-id>`
- **Secret token**: shown in the web UI after workspace registration (Workspaces tab)
- **Trigger**: Push events

Every push will now trigger a sync + reindex job visible in the Jobs tab.

---

## MCP tools

| Tool | Description |
|---|---|
| `retrieve_context` | Hybrid RAG query — returns context from the knowledge graph |
| `find_path` | Shortest path between two concepts |
| `rate_context` | Rate a result to improve future queries |
| `register_workspace` | Register a GitLab repo |
| `list_workspaces` | List registered workspaces |
| `unregister_workspace` | Remove a workspace and its indexed notes |
| `configure` | View or update embed/LLM config (`persist=true` saves to disk) |
| `setup` | One-shot provider setup wizard |
| `lookup_note` | Resolve an exact source path, mirrored note path, or symbol to its mirrored note |

Agent guidance: see [AGENTS_CONTEXT_GARDEN_BEST_PRACTICES.md](AGENTS_CONTEXT_GARDEN_BEST_PRACTICES.md).

### Querying

```
retrieve_context(
  query: "how does authentication work?",
  workspace: "my-project",   // optional — omit to search all
  max_chars: 15000
)
```

---

## Web UI

**http://localhost:7433**

- **Workspaces** — register repos, trigger manual sync/reindex, view webhook secrets
- **Jobs** — live job queue with logs for every sync and index run
- **Settings** — configure embedding and LLM providers (persisted to Docker volume)

---

## Data directory layout

```
data/
  md_db/
    code/
      <workspace-name>/     ← mirrored notes
  .context-garden/
    config.json             ← provider config
    workspaces.json         ← workspace registry
    secrets.env             ← API keys (never committed)
    knowledge_graph/        ← LlamaIndex vector + graph index
    clones/                 ← git clones of registered repos
```

---

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `CG_WEBAPP_PORT` | HTTP port | `7433` |
| `CG_GIT_SSH_COMMAND` | Full SSH command override for git | auto |
| `CG_GITLAB_TOKEN` | Fallback GitLab access token (if not per-workspace) | — |

Provider config (embed host, model, API keys) is managed via the Settings UI or `configure` MCP tool — not env vars.

---

## Troubleshooting

**No results from `retrieve_context`**
- Check that a workspace is registered: `list_workspaces`
- Indexing runs in background — check the Jobs tab for progress
- Verify Ollama is reachable: `curl http://localhost:11434/api/tags` (from the host)
- If running Ollama on the host, use `http://host.docker.internal:11434` as the embed host

**llama.cpp embeddings not working**
- Verify the OpenAI-compatible endpoint responds: `curl http://10.0.132.7:8080/v1/models`
- Use an actual model ID exposed by that server, for example `nomic`
- If a new image was published, pull and restart the container:
  `sudo docker compose pull context-garden && sudo docker compose up -d context-garden`

**Webhook not triggering sync**
- Confirm the secret token in GitLab matches the one shown in the Workspaces tab
- Check the container logs: `sudo docker compose logs -f`
- GitLab must be able to reach your host on port 7433

**SSH / git pull fails**
- Verify the key is mounted and readable: `sudo docker compose exec context-garden ls -la /run/secrets/git_ssh_key`
- Test SSH from inside the container: `sudo docker compose exec context-garden ssh -i /run/secrets/git_ssh_key -T git@your-gitlab-host`
- Ensure `known_hosts` contains your GitLab host's fingerprint

**Embedding model not loaded**
- On first query, Ollama pulls the model — this can take a minute
- Check: `ollama ps` on the host

**Settings not saving**
- Ensure the `./data` volume directory is writable
- Check logs: `sudo docker compose logs context-garden`

## Publishing a new container version

The repository publishes `ghcr.io/elderavo/context-garden`.

- Push to `master` on the `github` remote to refresh the `latest` tag.
- Create and push a Git tag like `v0.1.1` to publish immutable tags `0.1.1` and `0.1`.

Example:

```bash
git tag v0.1.1
git push github master
git push github v0.1.1
```

After the GitHub Actions workflow completes, users can update with:

```bash
sudo docker compose pull context-garden
sudo docker compose up -d context-garden
```
