# Personal Memory Server: Home Setup

This fork turns the Graphiti MCP server into one shared, long-term memory for all of
your AI agents (Claude, Grok, OpenMausBot, ...). Every agent reads and writes the same
temporal knowledge graph, so a fact told to one agent is available to the others, and a
newer fact automatically supersedes an older one while the history is kept.

## What this fork adds on top of upstream Graphiti

| Addition | Where |
| --- | --- |
| `remember`, `recall`, `fact_history`, `list_categories` tools, designed for agents to call routinely | `src/graphiti_mcp_server.py` |
| Category taxonomy with keyword inference and related-category recall (a mortgage question also returns finance and employment facts) | `config/config-home.yaml`, `src/services/category_service.py` |
| Current-only recall by default, plus `as_of` point-in-time recall and full history | `src/services/recall_service.py` |
| Notes: full text kept, `get_note` / `search_notes` tools, source notes with excerpts on every recalled fact, long notes split into linked parts | `src/services/notes_service.py` |
| Secret URL path for the MCP endpoint, and Host-header checking for your Tailscale hostname | `src/utils/access.py` |
| Owner name: "I"/"my" from any agent resolves to the same person entity | `graphiti.owner_name` |
| Home Docker Compose (localhost-only ports, persistent volume) with FalkorDB pinned to 4.16 | `docker/docker-compose-home.yml` |

All upstream tools (`add_memory`, `search_memory_facts`, ...) are still available.

## How memories are organised

- **One graph.** Every memory goes into a single group (`GRAPHITI_GROUP_ID`, default
  `personal`), so facts in different areas of life stay linked (your employer connects
  to your salary, which connects to your mortgage affordability).
- **Categories are tags.** `remember` tags the memory with categories (given by the
  agent, or inferred from keywords). Every fact extracted from that memory inherits them.
- **Related categories.** Each category lists related ones in `config/config-home.yaml`.
  `recall("remortgage options")` infers `property` and expands it to `property`, `finance`
  and `employment`. It then runs one hybrid search (semantic + keyword + graph) for the
  query and one per category, and merges the results.
- **Notes.** Agents can `remember` anything from a one-line fact to a long note (finances,
  personal circumstances, plans). The full text is kept, and facts are extracted from it.
  Every fact `recall` returns lists the notes it came from, with the supporting sentence;
  `get_note` reads a note in full and `search_notes` finds notes by keyword or meaning.
  Notes longer than `note_part_chars` (2,000 characters by default) are split at headings
  and paragraphs into linked parts. Each part gets its own fact extraction and categories,
  and `get_note` reassembles them.
- **Time.** Each fact carries `valid_at` / `invalid_at`. `recall` returns only facts that
  are true now. `fact_history` shows superseded facts too, oldest first. `recall(as_of=...)`
  answers "what was true on that date".

Edit categories, keywords and relations in `config/config-home.yaml`, then restart the
container. Agents can also use ad-hoc category names.

## 1. Install on the home server

Requirements: Docker with Compose, Tailscale, and an OpenAI API key (used for extraction
and embeddings; extraction can use Claude instead, see `.env.home.example`).

```bash
git clone https://github.com/GaryStimson/graphiti.git
cd graphiti/mcp_server
cp .env.home.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # paste into MCP_SECRET_PATH
nano .env                                                        # fill in the rest
docker compose --env-file .env -f docker/docker-compose-home.yml up -d --build
docker compose --env-file .env -f docker/docker-compose-home.yml logs -f   # wait for "MCP Server Access Information"
curl http://127.0.0.1:8000/health
```

Always pass `--env-file .env` (including for `up`, `down`, `restart`): Compose only reads
`${MCP_HOST_PORT}` from there, not from the container's `env_file`.

The logs never print the full secret. `http://127.0.0.1:8000/mcp` returns 404 by design.

If something else already uses port 8000 on the host, set `MCP_HOST_PORT=8100` (any free
port) in `.env` and use that port in the health check and the Funnel command below.

## 2. Expose it with Tailscale Funnel

Funnel can only publish on ports 443, 8443 and 10000. Use 10000 so the memory server
does not collide with services already on 443 or 8443:

```bash
sudo tailscale funnel --bg --https=10000 http://127.0.0.1:8000
tailscale funnel status
```

(On Windows there is no `sudo`; run `tailscale funnel ...` directly. Funnel on a non-443
port sends `Host: <machine>.<tailnet>.ts.net:10000`; the server accepts the host with any port.)

Your memory URL is:

```
https://<machine>.<tailnet>.ts.net:10000/<MCP_SECRET_PATH>/mcp
```

If all three Funnel ports are already in use, mount the server under its secret path on
a port you already publish instead. Funnel strips the mount path and appends the rest of
the path to the target, so the server still sees `/<secret>/mcp`:

```bash
sudo tailscale funnel --bg --https=443 --set-path=/<MCP_SECRET_PATH> \
  http://127.0.0.1:8000/<MCP_SECRET_PATH>
# URL: https://<machine>.<tailnet>.ts.net/<MCP_SECRET_PATH>/mcp
```

Set `MCP_ALLOWED_HOSTS=<machine>.<tailnet>.ts.net` in `.env` (then restart) so requests
with any other Host header are rejected.

Never funnel port 3000. The FalkorDB browser is only bound to localhost; reach it from
your tailnet with `tailscale serve` if you want to explore the graph visually.

## 3. Connect your agents

Use the same URL everywhere.

- **Claude (claude.ai, Desktop, mobile):** Settings → Connectors → Add custom connector →
  paste the URL. Leave the OAuth fields empty.
- **Claude Code:** `claude mcp add --transport http memory "https://…:10000/<secret>/mcp"`
- **Grok (xAI API) / bots built on it:** add a remote MCP tool with `server_url` set to the
  URL.
- **OpenMausBot or any other MCP client:** add a Streamable HTTP MCP server with the URL.
  If a client only supports stdio, bridge it with
  `npx mcp-remote "https://…:10000/<secret>/mcp"`.

### Recommended agent instructions

The server already sends usage instructions to every client. For agents that support a
system prompt or custom instructions, also add:

> You share a long-term memory with my other AI assistants through the `memory` MCP server.
> Before answering anything about me, my plans, finances, home, work, health, family or
> hobbies, call `recall` with the topic, and use `get_note` when a fact's source note would
> give useful detail. Whenever I tell you a lasting fact, preference, decision or change,
> call `remember` with a short self-contained statement (or a fuller note with headings), the
> relevant categories, and `agent` set to your name. When something changes, just
> remember the new fact; older facts are superseded automatically.

## Security model

- The secret path acts as a password inside a URL. Anyone with the full URL can read and
  write your memory. Keep it out of screenshots, public repos and shared chats. Rotate it
  by changing `MCP_SECRET_PATH` and restarting, then update each agent.
- Funnel serves HTTPS only, so the secret is never sent in clear text over the internet.
- Every path except `/<secret>/mcp` and `/health` returns 404.
- `MCP_ALLOWED_HOSTS` enables DNS rebinding protection (wrong Host header → 421).
- Memory contents are sent to your LLM provider for extraction, and to the agents that
  recall them. Don't store passwords, card numbers or one-time codes.

## Operations

- **Backup:** `docker exec <container> redis-cli BGSAVE`, then
  `docker run --rm -v personal_memory_data:/data -v "$PWD":/backup alpine tar czf /backup/memory-$(date +%F).tgz -C /data .`
- **Cost:** each `remember` makes a handful of small LLM calls plus embeddings per note
  part, so a long note costs roughly one short memory per 2,000 characters. `recall`
  only makes embedding calls.
- **Rate limits:** lower `SEMAPHORE_LIMIT` if the logs show 429 errors.
- **Updating from upstream:** use GitHub's "Sync fork" on the `main` branch, then merge
  `main` into your working branch.
