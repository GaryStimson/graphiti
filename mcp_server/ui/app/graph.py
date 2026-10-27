"""Read access to the FalkorDB graph and the in-memory snapshot the views are built from.

A personal memory graph is small (hundreds to a few thousand nodes), so each view loads one
snapshot (episodes, facts, entities, mentions) and filters it in Python. Embeddings are never
fetched. The snapshot is cached for a few seconds and dropped after every write.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from falkordb import FalkorDB

from .parsing import (
    fact_status,
    parse_agent,
    parse_categories,
    parse_dt,
    parse_note_ref,
)

CACHE_SECONDS = 3.0


class GraphStore:
    def __init__(self, host: str, port: int, password: str, graph_name: str):
        self._db = FalkorDB(host=host, port=port, password=password or None)
        self._name = graph_name
        self._cache: tuple[float, Snapshot] | None = None
        self._lock = Lock()

    def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        graph = self._db.select_graph(self._name)
        return graph.query(cypher, params or {}).result_set

    def ping(self) -> bool:
        try:
            self._db.select_graph(self._name).query('RETURN 1')
            return True
        except Exception:
            return False

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def snapshot(self, fresh: bool = False) -> Snapshot:
        with self._lock:
            if not fresh and self._cache and time.time() - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
        snap = self._load()
        with self._lock:
            self._cache = (time.time(), snap)
        return snap

    def _load(self) -> Snapshot:
        episodes = [
            Episode(
                uuid=r[0],
                name=r[1] or '',
                source_description=r[2] or '',
                content=r[3] or '',
                created_at=parse_dt(r[4]),
                valid_at=parse_dt(r[5]),
            )
            for r in self.query(
                'MATCH (e:Episodic) RETURN e.uuid, e.name, e.source_description, e.content,'
                ' e.created_at, e.valid_at'
            )
        ]
        facts = [
            Fact(
                uuid=r[0],
                relation=r[1] or '',
                fact=r[2] or '',
                episodes=list(r[3] or []),
                created_at=parse_dt(r[4]),
                valid_at=parse_dt(r[5]),
                invalid_at=parse_dt(r[6]),
                expired_at=parse_dt(r[7]),
                source_uuid=r[8],
                source_name=r[9] or '',
                target_uuid=r[10],
                target_name=r[11] or '',
            )
            for r in self.query(
                'MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) RETURN r.uuid, r.name, r.fact,'
                ' r.episodes, r.created_at, r.valid_at, r.invalid_at, r.expired_at,'
                ' a.uuid, a.name, b.uuid, b.name'
            )
        ]
        entities = [
            Entity(
                uuid=r[0],
                name=r[1] or '',
                labels=[label for label in (r[2] or []) if label != 'Entity'],
                summary=r[3] or '',
                created_at=parse_dt(r[4]),
            )
            for r in self.query(
                'MATCH (n:Entity) RETURN n.uuid, n.name, labels(n), n.summary, n.created_at'
            )
        ]
        mentions = [
            (r[0], r[1])
            for r in self.query('MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) RETURN e.uuid, n.uuid')
        ]
        return Snapshot(episodes, facts, entities, mentions, datetime.now(timezone.utc))


@dataclass
class Episode:
    uuid: str
    name: str
    source_description: str
    content: str
    created_at: datetime | None
    valid_at: datetime | None


@dataclass
class Fact:
    uuid: str
    relation: str
    fact: str
    episodes: list[str]
    created_at: datetime | None
    valid_at: datetime | None
    invalid_at: datetime | None
    expired_at: datetime | None
    source_uuid: str
    source_name: str
    target_uuid: str
    target_name: str


@dataclass
class Entity:
    uuid: str
    name: str
    labels: list[str]
    summary: str
    created_at: datetime | None


@dataclass
class Note:
    note_id: str
    title: str
    categories: list[str]
    agent: str | None
    recorded_at: datetime | None
    parts: list[Episode]

    @property
    def content(self) -> str:
        return '\n\n'.join(part.content for part in self.parts)

    @property
    def episode_uuids(self) -> list[str]:
        return [part.uuid for part in self.parts]


@dataclass
class Snapshot:
    episodes: list[Episode]
    facts: list[Fact]
    entities: list[Entity]
    mentions: list[tuple[str, str]]
    now: datetime
    notes: dict[str, Note] = field(default_factory=dict)
    episode_note: dict[str, str] = field(default_factory=dict)
    entities_by_uuid: dict[str, Entity] = field(default_factory=dict)
    facts_by_uuid: dict[str, Fact] = field(default_factory=dict)

    def __post_init__(self) -> None:
        grouped: dict[str, list[tuple[int, Episode]]] = {}
        for episode in self.episodes:
            ref = parse_note_ref(episode.source_description)
            note_id = ref.note_id if ref else episode.uuid
            self.episode_note[episode.uuid] = note_id
            grouped.setdefault(note_id, []).append((ref.part if ref else 1, episode))
        for note_id, parts in grouped.items():
            parts.sort(key=lambda item: item[0])
            ordered = [episode for _, episode in parts]
            first = ordered[0]
            categories: list[str] = []
            for episode in ordered:
                for category in parse_categories(episode.source_description):
                    if category not in categories:
                        categories.append(category)
            self.notes[note_id] = Note(
                note_id=note_id,
                title=first.name,
                categories=categories,
                agent=parse_agent(first.source_description),
                recorded_at=first.created_at,
                parts=ordered,
            )
        self.entities_by_uuid = {e.uuid: e for e in self.entities}
        self.facts_by_uuid = {f.uuid: f for f in self.facts}

    def status(self, fact: Fact) -> str:
        return fact_status(fact.valid_at, fact.invalid_at, fact.expired_at, self.now)

    def fact_notes(self, fact: Fact) -> list[Note]:
        seen: dict[str, Note] = {}
        for episode_uuid in fact.episodes:
            note = self.notes.get(self.episode_note.get(episode_uuid, ''))
            if note and note.note_id not in seen:
                seen[note.note_id] = note
        return list(seen.values())
