"""Selection and formatting of recalled facts.

The recall tool runs several hybrid searches (one for the query itself and one per
relevant category) and hands the hits to ``select_facts``, which applies the
temporal view (current facts only, or full history) and the category filter.

This module is free of I/O so it can be unit-tested without a database or LLM.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from graphiti_core.edges import EntityEdge

from services.notes_service import excerpt

FactStatus = Literal['current', 'superseded', 'not_yet_valid']

QUERY_SOURCE = 'query'
MAX_SOURCES = 3


@dataclass
class EpisodeTags:
    """Categories, writing agents and source note of the episodes behind a fact."""

    categories: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    note_id: str | None = None
    title: str | None = None
    recorded_at: str | None = None
    content: str | None = None


def _aware(value: datetime | None) -> datetime | None:
    """Treat timezone-naive datetimes from the database as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def fact_status(edge: EntityEdge, as_of: datetime) -> FactStatus:
    """Classify a fact relative to a point in time.

    A fact is superseded once its invalid_at has passed, or, when the real-world end
    date is unknown, once graphiti has expired it because newer information
    contradicted it.
    """
    valid_at, invalid_at, expired_at = (
        _aware(edge.valid_at),
        _aware(edge.invalid_at),
        _aware(edge.expired_at),
    )
    if valid_at is not None and valid_at > as_of:
        return 'not_yet_valid'
    if invalid_at is not None:
        return 'superseded' if invalid_at <= as_of else 'current'
    if expired_at is not None and expired_at <= as_of:
        return 'superseded'
    return 'current'


def tags_for_edge(edge: EntityEdge, episode_tags: dict[str, EpisodeTags]) -> EpisodeTags:
    """Union the tags of every episode that supports the fact."""
    combined = EpisodeTags()
    for episode_uuid in edge.episodes or []:
        tags = episode_tags.get(episode_uuid)
        if tags is None:
            continue
        for category in tags.categories:
            if category not in combined.categories:
                combined.categories.append(category)
        for agent in tags.agents:
            if agent not in combined.agents:
                combined.agents.append(agent)
    return combined


def sources_for_edge(
    edge: EntityEdge, episode_tags: dict[str, EpisodeTags], max_sources: int = MAX_SOURCES
) -> list[dict[str, Any]]:
    """The notes a fact came from, newest first, with the passage that supports it.

    Pass a source's note_id to get_note to read the whole note.
    """
    sources: dict[str, dict[str, Any]] = {}
    for episode_uuid in edge.episodes or []:
        tags = episode_tags.get(episode_uuid)
        if tags is None or tags.content is None:
            continue
        note_id = tags.note_id or episode_uuid
        if note_id in sources:
            continue
        sources[note_id] = {
            'note_id': note_id,
            'title': tags.title,
            'recorded_at': tags.recorded_at,
            'agent': tags.agents[0] if tags.agents else None,
            'excerpt': excerpt(tags.content, edge.fact),
        }
    ordered = sorted(sources.values(), key=lambda s: s['recorded_at'] or '', reverse=True)
    return ordered[:max_sources]


def format_recalled_fact(
    edge: EntityEdge,
    status: FactStatus,
    tags: EpisodeTags,
    matched: list[str],
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compact, agent-friendly view of a fact."""
    return {
        'uuid': edge.uuid,
        'fact': edge.fact,
        'relation': edge.name,
        'status': status,
        'valid_at': edge.valid_at.isoformat() if edge.valid_at else None,
        'invalid_at': edge.invalid_at.isoformat() if edge.invalid_at else None,
        'recorded_at': edge.created_at.isoformat() if edge.created_at else None,
        'categories': tags.categories,
        'agents': tags.agents,
        'matched': matched,
        'sources': sources or [],
    }


def select_facts(
    hits: list[tuple[EntityEdge, str]],
    episode_tags: dict[str, EpisodeTags],
    categories: list[str],
    strict_categories: bool,
    include_history: bool,
    as_of: datetime,
    limit: int,
    include_upcoming: bool = False,
) -> list[dict[str, Any]]:
    """Filter, de-duplicate and format search hits in priority order.

    Args:
        hits: (edge, source) pairs in priority order. source is QUERY_SOURCE for hits
            of the query itself, or the category name whose search produced the hit.
        episode_tags: Tags of the episodes referenced by the hits, keyed by episode UUID.
        categories: The (expanded) categories the caller is interested in.
        strict_categories: When True every fact must carry one of ``categories``.
            When False, query hits are always kept and only category-search hits must
            carry the category that found them. Untagged facts (written outside
            ``remember``) never fail the category check.
        include_history: Keep superseded and not-yet-valid facts.
        as_of: The point in time used to decide whether a fact is current.
        limit: Maximum number of facts to return.
        include_upcoming: Keep not-yet-valid facts (plans, future dates) even without
            include_history; they carry status "not_yet_valid". Superseded facts still
            need include_history.
    """
    wanted = set(categories)
    selected: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for edge, source in hits:
        tags = tags_for_edge(edge, episode_tags)
        if tags.categories:
            if strict_categories and not wanted.intersection(tags.categories):
                continue
            if source != QUERY_SOURCE and source not in tags.categories:
                continue

        if edge.uuid in selected:
            if source not in selected[edge.uuid]['matched']:
                selected[edge.uuid]['matched'].append(source)
            continue

        status = fact_status(edge, as_of)
        keep = status == 'current' or (include_upcoming and status == 'not_yet_valid')
        if not keep and not include_history:
            continue

        if len(order) >= limit:
            continue
        selected[edge.uuid] = format_recalled_fact(
            edge, status, tags, [source], sources_for_edge(edge, episode_tags)
        )
        order.append(edge.uuid)

    return [selected[uuid] for uuid in order]


def sort_history(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order facts oldest to newest by when they became true (then when recorded)."""
    return sorted(facts, key=lambda fact: (fact['valid_at'] or '', fact['recorded_at'] or ''))
