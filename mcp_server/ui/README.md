# Memory Manager UI

A small, password-protected web app that runs beside the personal memory server. Use it to see
everything your agents have stored, spot bad memories, fix or delete them without going through
an agent, and explore the knowledge graph. It installs as an app (PWA) on phones, tablets and PCs.

```
browser / installed app ──► memory-ui (FastAPI + static SPA, port 8200)
                                 ├─► FalkorDB      reads the graph directly (compose network)
                                 └─► MCP server    "remember" for add / edit (same extraction as agents)
```

It never holds the OpenAI key and never writes embeddings: anything that needs the LLM goes through
the memory server's own `remember` tool.

## Run it

It is part of `docker/docker-compose-home.yml`.

1. Set a password in `mcp_server/.env` (at least 12 characters; use a long random one if it is reachable from the internet):

   ```
   UI_PASSWORD=...
   ```

2. From `mcp_server`:

   ```bash
   docker compose --env-file .env -f docker/docker-compose-home.yml up -d --build
   ```

3. Open <http://127.0.0.1:8200> on the machine that runs it and sign in.

If `UI_PASSWORD` is missing or shorter than 12 characters the login is refused (the app fails closed).

Settings (all optional, in `.env`): `UI_HOST_PORT` (default 8200), `UI_ALLOWED_HOSTS`
(comma-separated Host names the UI accepts, default `localhost,127.0.0.1`), `UI_SESSION_DAYS`.

## What it does

| Page | |
|---|---|
| Overview | counts, categories, agents, what was added this week, export |
| Notes | search and filter (category, agent, time); note text, its facts and entities; delete note |
| Facts | current / upcoming / superseded, filter by category, agent, text; edit wording, "no longer true", restore, delete |
| Graph | entity graph with facts as labelled lines (dashed = superseded, dotted = upcoming); search, centre on an entity, 1-3 hops, category and status filters, save as PNG. The most connected entity (usually you) is hidden by default in the overview because it links to everything. |
| Review | possibly sensitive notes and facts (card numbers, passwords, one-time codes, API keys, bank details, NI numbers), recent additions by agent, duplicates, facts without a source, notes with no facts |
| Add | send a memory to the server exactly like an agent would |
| History | every change made from the UI |

### Changing memories

- **Delete note**: removes the note (all its parts), the facts that exist only because of it, and
  entities left with nothing. Facts also supported by another note are kept. The confirmation lists
  exactly what will go.
- **Delete fact**: removes one fact; the source note stays.
- **No longer true / Restore**: sets or clears the end of a fact, keeping history.
- **Edit wording**: the old fact is deleted and your corrected sentence is added with `remember`, so
  the server extracts it again. Tick "also delete the source note" if the old wording would
  otherwise be extracted again from it.
- Not in v1: renaming or merging entities, editing note text in place.

Safety: every delete first saves a recovery copy (JSON) to the `memory_ui_data` volume under
`trash/`, and every change is appended to `audit.jsonl` there. There is no one-click undo for
deletes; recovery copies are restored by hand. Export everything from the Overview page.

The sensitive-data scan is advisory only: it warns and never deletes. Memory should not hold
passwords, card numbers or one-time codes.

## Security model

- Single owner, password from `UI_PASSWORD`. Sessions are signed cookies (HttpOnly, SameSite=Strict,
  `Secure` when served over HTTPS). The signing key is generated once and kept in the data volume.
- Failed logins are throttled (5 per client, then a 15 minute lockout).
- State-changing calls need a custom header and a same-origin `Origin`, on top of SameSite=Strict.
- A Host allow-list (`UI_ALLOWED_HOSTS`) guards against DNS rebinding; unknown hosts get 421.
- Strict Content-Security-Policy (no inline script or style), `no-store` on API responses, nothing
  secret is ever sent to the browser. Memory text is untrusted and always rendered as plain text.
- Published on `127.0.0.1` only. The container is read-only, drops all capabilities and runs as a
  non-root user. FalkorDB's port is not published to the host.

**Do not put this on the public internet with only the password** (no Funnel, no public tunnel):
it can delete everything. For remote use, keep it on your tailnet or behind Cloudflare Access.

## Sharing a Cloudflare hostname with the MCP server

The UI can live on the same hostname as the MCP endpoint (for example `memory.example.com`).
In the tunnel's `config.yml`, put the secret MCP path first and the UI as the fallback:

```yaml
ingress:
  - hostname: memory.example.com
    path: ^/<MCP_SECRET_PATH>/
    service: http://127.0.0.1:8100      # MCP server (MCP_HOST_PORT)
  - hostname: memory.example.com
    service: http://127.0.0.1:8200      # Memory Manager UI
```

and in `.env`:

```
UI_ALLOWED_HOSTS=localhost,127.0.0.1,memory.example.com
UI_TRUSTED_IP_HEADER=cf-connecting-ip   # lockout per real visitor, not per tunnel
```

Check the rules with `cloudflared tunnel ingress rule https://memory.example.com/` and restart the
tunnel service. Because this puts a delete-capable UI on the internet behind only a password:
use a long random `UI_PASSWORD` (12+ characters is enforced), add a Cloudflare rate-limiting rule
for `POST /auth/login`, and consider Cloudflare Access for real multi-factor protection.

## Installing as an app

Installing needs a secure origin (`https://`, or `localhost` on the same machine). On the machine
that runs it, open <http://localhost:8200> in Chrome or Edge and use the browser's Install button
(or the "Install app" button in the page header).

To install on other devices (phone, tablet, message-board style display), give the UI an HTTPS
address on your tailnet and allow that host name. Tailscale can do this without exposing anything
publicly:

```bash
tailscale serve --bg --https=8201 http://127.0.0.1:8200     # tailnet only, NOT funnel
```

then add the name to `UI_ALLOWED_HOSTS` in `.env`
(for example `UI_ALLOWED_HOSTS=localhost,127.0.0.1,<machine>.<tailnet>.ts.net`), recreate the
container, and open `https://<machine>.<tailnet>.ts.net:8201` on the device:

- Android / Chrome: menu, "Install app" (or the Install button in the header).
- iPhone / iPad (Safari): Share, "Add to Home Screen".
- Windows / macOS (Chrome, Edge): the install icon in the address bar.

The installed app opens full-screen with its own icon. The service worker caches only the static
shell (never any memory data), so a signed-out or offline device shows nothing private.

## Development

```bash
cd mcp_server/ui
pip install -r requirements.txt pytest
python -m pytest            # unit tests; no database needed (an in-memory fake store is used)
```

`app/` is the FastAPI backend (`graph.py` reads the snapshot, `domain.py` builds the views,
`ops.py` does the changes, `auth.py` and `main.py` the security layer); `static/` is the single-page
app (no build step) with a vendored copy of Cytoscape.js (MIT).

Limits: each request loads the whole graph into memory, which suits a personal graph (thousands of
nodes). If yours grows far beyond that, move the filtering into Cypher queries.
