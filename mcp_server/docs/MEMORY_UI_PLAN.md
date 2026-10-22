# Memory Manager UI: project plan

A small web front end that runs alongside the personal memory server so Gary can
see everything the agents have stored, fix or delete bad memories without relying on an
agent, and explore the knowledge graph visually.

Status: **plan agreed, not started.** Decisions made are recorded in section 10.

## 1. Goals and non-goals

Goals
1. Browse all notes, facts, entities and categories, filterable by category, agent, date
   and status (current / superseded / upcoming).
2. Spot and fix bad memories: delete a note or a fact, edit a fact's wording, end a fact
   ("no longer true"), restore a wrongly superseded fact, rename or merge entities.
3. Graphical view of the knowledge graph (entities as nodes, facts as labelled edges),
   with click-through to the source notes.
4. Review feed: "what did agents add recently", so bad writes are caught early.
5. Runs as one more container in `docker-compose-home.yml`, localhost / tailnet only.

Non-goals (for v1)
- Replacing the MCP tools for agents. Agents keep using `remember` / `recall`.
- Multi-user accounts, roles or OAuth. Single owner.
- Editing raw Cypher (FalkorDB Browser on `127.0.0.1:3000` already covers that).

## 2. What is in the graph (verified on the live instance)

- Labels: `Episodic` (a note or note part), `Entity` (people, organisations, assets, ...),
  `Community`, `Saga`.
- Relationships: `RELATES_TO` (a **fact**, with `fact`, `name`, `valid_at`, `invalid_at`,
  `expired_at`, `episodes[]`, `fact_embedding`), `MENTIONS` (note to entity),
  `NEXT_EPISODE`, `HAS_MEMBER`, `HAS_EPISODE`.
- Note metadata (agent, categories, note id, part n/N) is encoded in each episode's
  `source_description`; `services/notes_service.py` and `category_service.py` already parse it.
- Long notes are several `Episodic` nodes sharing a note id; the UI must treat them as one note.
- Fact status (current / superseded / not_yet_valid) is computed by
  `services/recall_service.fact_status`; the UI reuses it rather than re-implementing it.

## 3. Architecture

```
browser ──► memory-ui (FastAPI + static SPA) ──► FalkorDB  (personal-memory:6379, compose network)
                       └─────────────────────► MCP server (compose network): remember, for add / edit
```

- **New compose service `memory-ui`**, same stack, own image, port published as
  `127.0.0.1:${UI_HOST_PORT:-8200}:8200`. Not funnelled or tunnelled by default.
- **Backend: Python / FastAPI**, reusing `graphiti-core` models (`EntityEdge`, `EntityNode`,
  `EpisodicNode`) and the existing service modules, so parsing and status logic is not
  duplicated. Talks to FalkorDB directly over the compose network. Port 6379 is **not**
  published to the host (keeps the database private).
- **Frontend: static SPA** served by the same FastAPI app. Vanilla JS or Preact, no build
  step if possible. Graph view with **Cytoscape.js** (handles a few thousand nodes, layouts,
  filtering) loaded from a pinned local copy, not a CDN.
- Shared code: move the pure helpers into a small package both containers import, or
  copy the image layer (decide in milestone 0).

### Why direct DB access, and the risk

Direct access gives list/browse/delete operations the MCP tools do not offer
(`get_episodes` ordering is unreliable). The risk is concurrent writes while the MCP
server's ingest queue is working. Mitigation: UI writes are small, single-statement
operations, and the UI shows a banner when `get_status` / queue depth indicates ingestion is
in progress (bulk actions are blocked while it is).

## 4. Features and exact semantics

### 4.1 Browse
- **Notes list:** title, categories, agent, recorded_at, parts, number of facts; search box
  (keyword + the existing meaning search through the server's embedder), filters.
- **Note detail:** full text (reassembled from parts), the facts extracted from it, entities
  mentioned, and raw `source_description`.
- **Facts list:** fact text, subject, relation, object, status badge, valid_at, invalid_at,
  source notes, agent. Filter on status; default view hides superseded.
- **Entities list:** name, labels, summary, number of facts; detail page with all connected facts.
- **Timeline:** `fact_history` style view for any entity or search term.

### 4.2 Fix and manage
| Action | What it does | Notes |
|---|---|---|
| Delete note | Removes the note (all parts), the facts that exist only because of it, and entities left with no facts. Uses Graphiti's episode removal semantics. | Confirmation dialog shows exactly what will be removed. |
| Delete fact | Removes one `RELATES_TO` edge. | Source note stays. |
| Edit fact wording | **Delete and re-add:** the old fact is deleted, and the corrected sentence is submitted through the MCP server's `remember` (same extraction and embeddings as agents). | The UI never holds an OpenAI key and never writes embeddings. The confirmation shows the old fact, the new sentence and the categories; the new fact appears after background processing. |
| End a fact | Sets `invalid_at` (and `expired_at`): "no longer true", keeps history. | Date picker, defaults to now. |
| Restore a fact | Clears `invalid_at` / `expired_at`. | For facts wrongly superseded by extraction. |
| Rename / merge entities | **Deferred (not in v1).** Both need new embeddings or graph surgery. Workaround: delete the wrong facts and re-add them through `remember`. | Revisit after v1. |
| Add memory | Calls the MCP server's `remember`, so extraction is identical to agents'. | Not written directly into the graph. |
| Edit note text | **Not offered.** Facts were extracted from the old text, so an edit would leave them stale. Offer "replace note": delete + `remember` the new text, with a diff preview. | Explained in the UI. |

Safety for every write:
- Confirmation with a precise summary of the change.
- **Undo log:** every UI change appends a JSON record (before / after) to a table or file in the
  data volume; a "recent changes" page can revert the last N.
- **Snapshot / export:** one-click JSON export of the whole graph, plus a documented backup
  command for the volume. An automatic snapshot is taken before any bulk delete.

### 4.3 Review feed
- "Added in the last 24h / 7d", grouped by agent, with quick delete / edit.
- Flags (computed, advisory only): fact with no source note, duplicate-looking facts,
  facts whose supporting sentence does not contain the key entity, entities with a single
  weak mention, notes with sensitive patterns (card numbers, passwords, one-time codes)
  that must never be stored.

### 4.4 Graph view
- Nodes = entities (colour by label, size by number of facts); edges = facts (label = relation,
  dashed = superseded, dotted = upcoming).
- Click a node or edge: side panel with the fact, status, source notes and actions from 4.2.
- Controls: category filter, status filter, "centre on entity" with N-hop expansion,
  text search that focuses a node, layout switch (force / concentric / hierarchical),
  export PNG/SVG.
- Performance: server-side paging and neighbourhood queries; never load the whole graph by default.
  Start from search or the most-connected entities and expand on demand.

## 5. API sketch (backend)

```
GET  /api/stats                      counts, queue state, last write
GET  /api/notes?query=&category=&agent=&since=&page=
GET  /api/notes/{note_id}            full text + facts + entities
DELETE /api/notes/{note_id}
GET  /api/facts?status=&category=&agent=&query=&page=
PATCH /api/facts/{uuid}              {invalid_at?} (end / restore only)
POST /api/facts/{uuid}/rewrite       {new_text, categories?} delete + remember via the MCP server
DELETE /api/facts/{uuid}
GET  /api/entities?query=&page=      GET /api/entities/{uuid}   (read-only in v1)
GET  /api/graph?center=&hops=&status=&category=&limit=
POST /api/remember                   proxies to the MCP server's remember tool
GET  /api/changes   POST /api/changes/{id}/revert
GET  /api/export
```

## 6. Security

- **v1 is local only:** bound to `127.0.0.1` on the NUC, with no Funnel, tunnel or tailnet exposure.
  If remote access is wanted later: tailnet-only (`tailscale serve`, **not** Funnel) or a Cloudflare
  Tunnel hostname protected by **Cloudflare Access** (login required). Never expose it with only a
  secret URL: the UI can delete everything.
- Optional built-in HTTP basic auth or a single owner password (`UI_PASSWORD` in `.env`) as a
  second layer; CSRF protection on state-changing calls; strict `Host` allow-list like the
  MCP server.
- No secrets (API keys, MCP secret path) are ever sent to the browser or shown in the UI.
- All text is rendered escaped (memories contain agent-written content: treat as untrusted).
- Audit: every write is logged with time and the change.

## 7. Testing

- Unit tests for the pure logic (status, grouping parts into notes, delete-impact calculation,
  merge planning), in the style of `tests/test_personal_memory.py`.
- Integration tests against a throwaway FalkorDB container with a seeded graph
  (including a superseded fact, a future-dated fact, a 5-part note).
- Manual end-to-end checklist run against the real stack after each milestone: remember via MCP,
  see it in the UI, edit, delete, confirm `recall` agrees.
- Verify a deleted note does not reappear in `recall`, `search_notes` or the graph view.

## 8. Milestones

0. **Spike (0.5 day):** confirm FalkorDB reachable from a second container; decide how the UI
   shares code with the server; confirm Graphiti episode-removal behaviour on a test graph.
1. **Read-only UI (1-2 days):** stats, notes, facts, entities, search, note detail, status badges.
   Compose service, `.env` settings, docs. Safe to ship on its own.
2. **Graph view (1-2 days):** neighbourhood graph with filters and the side panel.
3. **Safe edits (1-2 days):** delete note / fact, end / restore fact, rewrite fact via delete +
   `remember`, undo log, export.
4. **Review feed and sensitive-data flags, add-memory (1-2 days).**
5. **Hardening (1 day):** auth layer, CSRF, remote-access guide (tailnet / Cloudflare Access),
   backups, final test pass.

Estimated total: about 6-9 working days of focused effort; milestone 1 alone gives immediate value.

## 9. Risks

| Risk | Mitigation |
|---|---|
| UI write races with the ingest queue | Small atomic writes; block bulk actions while the queue is busy; show queue state. |
| A rewritten fact is re-derived from the original note later (or the rewrite is partly re-extracted) | Rewrites go through `remember`, so they are real notes; when the old fact came from a note, the UI offers to delete that note too. |
| Deleting a note removes facts that another note also supports | Only delete facts whose `episodes[]` contain just that note; the confirmation shows the exact list. |
| Graph too large to draw | Neighbourhood paging and limits; never render the full graph by default. |
| Exposing a delete-everything UI | Local-only by default; Access / tailnet + password if remote. |
| Upstream `graphiti-core` upgrade changes schema | Version is pinned in the Dockerfile; add a schema check test. |

## 10. Decisions

Agreed with Gary:
1. **Access:** local only (`127.0.0.1` on the NUC) for now.
2. **Edits:** go through delete and re-add via the MCP server's `remember`; the UI holds no
   OpenAI key and writes no embeddings.
3. **Edit scope:** "edit fact wording" is enough for v1; no "replace note" diff workflow yet.
4. **Sensitive-data flags:** yes, the review feed warns about likely card numbers, passwords and
   one-time codes (advisory only; never auto-delete).
5. **Repo layout:** `mcp_server/ui/` in this fork.
6. **Tech stack:** FastAPI + lightweight SPA (Cytoscape.js for the graph).

Deferred: entity rename / merge, remote access, multi-user auth.
