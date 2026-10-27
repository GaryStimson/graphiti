"""Changes made from the UI: delete notes and facts, end or restore a fact.

Every change is recorded in an audit log and anything deleted is first written to a
"trash" snapshot (JSON) in the data volume, so a mistake can be recovered by hand.
Editing a fact's wording is delete + re-add through the memory server (see main.py), so
embeddings and fact extraction are always produced by the server itself.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .domain import fact_dict, note_dict, _note_fact_counts
from .graph import Fact, GraphStore, Note, Snapshot
from .parsing import iso


class Audit:
    """Append-only JSONL log of UI changes plus trash snapshots of deleted data."""

    def __init__(self, data_dir: Path):
        self.log_path = data_dir / 'audit.jsonl'
        self.trash_dir = data_dir / 'trash'

    def record(self, action: str, target: str, summary: str, **extra: Any) -> dict[str, Any]:
        entry = {
            'id': secrets.token_hex(6),
            'at': datetime.now(timezone.utc).isoformat(),
            'action': action,
            'target': target,
            'summary': summary,
            **extra,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except OSError:
            pass
        return entry

    def entries(self, limit: int = 200) -> list[dict[str, Any]]:
        try:
            lines = self.log_path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return []
        items = []
        for line in lines[-limit:]:
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
        return list(reversed(items))

    def snapshot_deleted(self, kind: str, target: str, payload: dict[str, Any]) -> str | None:
        name = f'{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{kind}-{target[:8]}.json'
        try:
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            (self.trash_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        except OSError:
            return None
        return name


def _orphans(snap: Snapshot, entity_uuids: set[str], gone_facts: set[str], gone_eps: set[str]) -> list[str]:
    """Entities that would be left with no facts and no mentions."""
    result = []
    for uuid in entity_uuids:
        still_fact = any(
            uuid in (f.source_uuid, f.target_uuid) and f.uuid not in gone_facts
            for f in snap.facts
        )
        still_mentioned = any(e not in gone_eps and u == uuid for e, u in snap.mentions)
        if not still_fact and not still_mentioned:
            result.append(uuid)
    return result


def plan_note_delete(snap: Snapshot, note: Note) -> dict[str, Any]:
    """What deleting a note would remove, trim or keep."""
    eps = set(note.episode_uuids)
    deleted, trimmed = [], []
    for fact in snap.facts:
        overlap = eps.intersection(fact.episodes)
        if not overlap:
            continue
        if set(fact.episodes) <= eps:
            deleted.append(fact)
        else:
            trimmed.append(fact)
    gone_facts = {f.uuid for f in deleted}
    touched = {e for ep, e in snap.mentions if ep in eps}
    for fact in deleted:
        touched.update((fact.source_uuid, fact.target_uuid))
    orphans = _orphans(snap, touched, gone_facts, eps)
    return {
        'note': note_dict(snap, note, _note_fact_counts(snap)),
        'facts_deleted': [fact_dict(snap, f, with_sources=False) for f in deleted],
        'facts_kept': [fact_dict(snap, f, with_sources=False) for f in trimmed],
        'entities_deleted': [
            {'uuid': u, 'name': snap.entities_by_uuid[u].name}
            for u in orphans
            if u in snap.entities_by_uuid
        ],
        '_deleted': deleted,
        '_trimmed': trimmed,
        '_orphans': orphans,
        '_episodes': sorted(eps),
    }


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if not k.startswith('_')}


def _delete_edge(store: GraphStore, fact_uuid: str) -> None:
    store.query('MATCH ()-[r:RELATES_TO {uuid: $u}]->() DELETE r', {'u': fact_uuid})
    store.query(
        'MATCH (e:Episodic) WHERE $u IN e.entity_edges '
        'SET e.entity_edges = [x IN e.entity_edges WHERE x <> $u]',
        {'u': fact_uuid},
    )


def delete_note(store: GraphStore, audit: Audit, snap: Snapshot, note: Note) -> dict[str, Any]:
    plan = plan_note_delete(snap, note)
    trash = audit.snapshot_deleted(
        'note',
        note.note_id,
        {
            'note': note_dict(snap, note, _note_fact_counts(snap), full=True),
            'facts_deleted': plan['facts_deleted'],
            'facts_kept': plan['facts_kept'],
            'entities_deleted': plan['entities_deleted'],
        },
    )
    eps = set(plan['_episodes'])
    for fact in plan['_deleted']:
        _delete_edge(store, fact.uuid)
    for fact in plan['_trimmed']:
        remaining = [e for e in fact.episodes if e not in eps]
        store.query(
            'MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.episodes = $eps',
            {'u': fact.uuid, 'eps': remaining},
        )
    for episode_uuid in plan['_episodes']:
        store.query('MATCH (e:Episodic {uuid: $u}) DETACH DELETE e', {'u': episode_uuid})
    for entity_uuid in plan['_orphans']:
        store.query('MATCH (n:Entity {uuid: $u}) DETACH DELETE n', {'u': entity_uuid})
    store.invalidate()
    entry = audit.record(
        'delete_note',
        note.note_id,
        f'Deleted note "{note.title}" ({len(plan["_deleted"])} facts, '
        f'{len(plan["_orphans"])} entities)',
        trash=trash,
    )
    return {'ok': True, 'change': entry, 'plan': public_plan(plan)}


def plan_fact_delete(snap: Snapshot, fact: Fact) -> dict[str, Any]:
    orphans = _orphans(snap, {fact.source_uuid, fact.target_uuid}, {fact.uuid}, set())
    return {
        'fact': fact_dict(snap, fact),
        'entities_deleted': [
            {'uuid': u, 'name': snap.entities_by_uuid[u].name}
            for u in orphans
            if u in snap.entities_by_uuid
        ],
        '_orphans': orphans,
    }


def delete_fact(store: GraphStore, audit: Audit, snap: Snapshot, fact: Fact) -> dict[str, Any]:
    plan = plan_fact_delete(snap, fact)
    trash = audit.snapshot_deleted('fact', fact.uuid, public_plan(plan))
    _delete_edge(store, fact.uuid)
    for entity_uuid in plan['_orphans']:
        store.query('MATCH (n:Entity {uuid: $u}) DETACH DELETE n', {'u': entity_uuid})
    store.invalidate()
    entry = audit.record(
        'delete_fact', fact.uuid, f'Deleted fact: {fact.fact[:120]}', trash=trash
    )
    return {'ok': True, 'change': entry, 'plan': public_plan(plan)}


def end_fact(
    store: GraphStore, audit: Audit, fact: Fact, at: datetime | None = None
) -> dict[str, Any]:
    """Mark a fact as no longer true (keeps it in the history)."""
    now = datetime.now(timezone.utc)
    when = at or now
    store.query(
        'MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.invalid_at = $at, r.expired_at = $now',
        {'u': fact.uuid, 'at': when.isoformat(), 'now': now.isoformat()},
    )
    store.invalidate()
    entry = audit.record(
        'end_fact',
        fact.uuid,
        f'Ended fact: {fact.fact[:120]}',
        before={'invalid_at': iso(fact.invalid_at), 'expired_at': iso(fact.expired_at)},
    )
    return {'ok': True, 'change': entry}


def restore_fact(store: GraphStore, audit: Audit, fact: Fact) -> dict[str, Any]:
    """Make a superseded fact current again."""
    store.query(
        'MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.invalid_at = null, r.expired_at = null',
        {'u': fact.uuid},
    )
    store.invalidate()
    entry = audit.record(
        'restore_fact',
        fact.uuid,
        f'Restored fact: {fact.fact[:120]}',
        before={'invalid_at': iso(fact.invalid_at), 'expired_at': iso(fact.expired_at)},
    )
    return {'ok': True, 'change': entry}
