from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.graph import Entity, Episode, Fact, Snapshot  # noqa: E402

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
PASSWORD = 'correct horse battery'


def build_snapshot() -> Snapshot:
    def ep(uuid: str, name: str, source: str, content: str, days_ago: int) -> Episode:
        when = NOW - timedelta(days=days_ago)
        return Episode(uuid, name, source, content, when, when)

    episodes = [
        ep('e1', 'Mortgage', 'agent=claude; categories=finance; note=n1; part=1/1',
           'My mortgage lender is Halifax.', 2),
        ep('e2a', 'Lisbon plan', 'agent=grok; categories=travel; note=n2; part=1/2',
           '# Lisbon\nFlying in March 2027.', 1),
        ep('e2b', 'Lisbon plan', 'agent=grok; categories=travel; note=n2; part=2/2',
           'Staying at Hotel Alfama.', 1),
        ep('e3', 'Secret', 'agent=openmaus; categories=profile; note=n3; part=1/1',
           'My bank password is hunter2xyz and card 4111 1111 1111 1111.', 0),
    ]
    entities = [
        Entity('gary', 'Gary Stimson', ['Person'], 'The owner', NOW),
        Entity('halifax', 'Halifax', ['Organization'], '', NOW),
        Entity('lisbon', 'Lisbon', ['Location'], '', NOW),
        Entity('alfama', 'Hotel Alfama', ['Organization'], '', NOW),
        Entity('lonely', 'Unused', [], '', NOW),
    ]

    def fact(uuid: str, text: str, src: str, tgt: str, eps: list[str], **kw: Any) -> Fact:
        names = {e.uuid: e.name for e in entities}
        return Fact(
            uuid=uuid, relation=kw.pop('relation', 'RELATES'), fact=text, episodes=eps,
            created_at=kw.pop('created_at', NOW - timedelta(days=1)),
            valid_at=kw.pop('valid_at', None), invalid_at=kw.pop('invalid_at', None),
            expired_at=kw.pop('expired_at', None), source_uuid=src, source_name=names[src],
            target_uuid=tgt, target_name=names[tgt],
        )

    facts = [
        fact('f1', "Gary's mortgage lender is Halifax.", 'gary', 'halifax', ['e1'],
             relation='HAS_MORTGAGE_WITH'),
        fact('f2', "Gary's mortgage lender is Nationwide.", 'gary', 'halifax', ['e1'],
             invalid_at=NOW - timedelta(days=1), relation='OLD'),
        fact('f3', 'Gary is flying to Lisbon in March 2027.', 'gary', 'lisbon', ['e2a'],
             valid_at=NOW + timedelta(days=100)),
        fact('f4', 'Gary stays at Hotel Alfama.', 'gary', 'alfama', ['e2a', 'e2b']),
        fact('f5', 'Gary likes Lisbon.', 'gary', 'lisbon', ['e2a', 'e1']),
    ]
    mentions = [('e1', 'gary'), ('e1', 'halifax'), ('e2a', 'gary'), ('e2a', 'lisbon'),
                ('e2b', 'alfama'), ('e3', 'gary')]
    return Snapshot(episodes, facts, entities, mentions, NOW)


@pytest.fixture
def snap() -> Snapshot:
    return build_snapshot()


class FakeStore:
    """Stands in for GraphStore: serves a snapshot and records the write queries."""

    def __init__(self, snapshot: Snapshot):
        self._snap = snapshot
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.invalidations = 0

    def snapshot(self, fresh: bool = False) -> Snapshot:
        return self._snap

    def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        self.queries.append((cypher, params or {}))
        return []

    def invalidate(self) -> None:
        self.invalidations += 1

    def ping(self) -> bool:
        return True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ui_password=PASSWORD, data_dir=tmp_path, falkordb_host='x', falkordb_port=1,
        falkordb_password='', graph_name='personal', mcp_url='http://x:8000',
        mcp_secret_path='s' * 40, mcp_host_header='localhost:8000',
        allowed_hosts=['testserver', 'localhost'],
    )
