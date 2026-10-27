"""Reading the tags the memory server writes into each episode's source_description.

Mirrors services/notes_service.py and services/category_service.py in the server; the
format is ``agent=claude; categories=finance,property; note=<id>; part=2/5``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_NOTE = re.compile(r'(?:^|;\s*)note=([^;\s]+)')
_PART = re.compile(r'(?:^|;\s*)part=(\d+)/(\d+)')
_AGENT = re.compile(r'(?:^|;\s*)agent=([^;]*)')
_CATEGORIES = re.compile(r'(?:^|;\s*)categories=([^;]*)')


@dataclass(frozen=True)
class NoteRef:
    note_id: str
    part: int
    parts: int


def normalize_category(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.strip().lower()).strip('_')


def parse_note_ref(source_description: str | None) -> NoteRef | None:
    if not source_description:
        return None
    note = _NOTE.search(source_description)
    if not note:
        return None
    part = _PART.search(source_description)
    if not part:
        return NoteRef(note.group(1), 1, 1)
    return NoteRef(note.group(1), int(part.group(1)), int(part.group(2)))


def parse_agent(source_description: str | None) -> str | None:
    match = _AGENT.search(source_description or '')
    return match.group(1).strip() or None if match else None


def parse_categories(source_description: str | None) -> list[str]:
    match = _CATEGORIES.search(source_description or '')
    if not match:
        return []
    return [c for c in (normalize_category(c) for c in match.group(1).split(',')) if c]


def parse_dt(value: Any) -> datetime | None:
    """Datetimes come back from FalkorDB as ISO strings (or None); treat naive as UTC."""
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def fact_status(
    valid_at: datetime | None,
    invalid_at: datetime | None,
    expired_at: datetime | None,
    now: datetime,
) -> str:
    """Same rules as the server's recall: current / superseded / not_yet_valid."""
    if valid_at is not None and valid_at > now:
        return 'not_yet_valid'
    if invalid_at is not None:
        return 'superseded' if invalid_at <= now else 'current'
    if expired_at is not None and expired_at <= now:
        return 'superseded'
    return 'current'
