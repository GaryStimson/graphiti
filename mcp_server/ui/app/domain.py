"""Views over a graph snapshot: notes, facts, entities, the graph, stats and the review feed."""

from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from datetime import timedelta
from typing import Any

from .graph import Entity, Fact, Note, Snapshot
from .parsing import iso, parse_agent, parse_categories
from .sensitive import scan

PER_PAGE = 25
EXCERPT = 200


def _page(items: list[Any], page: int, per_page: int = PER_PAGE) -> dict[str, Any]:
    page = max(1, page)
    start = (page - 1) * per_page
    return {
        'items': items[start : start + per_page],
        'total': len(items),
        'page': page,
        'pages': max(1, -(-len(items) // per_page)),
    }


def _contains(haystack: str, query: str) -> bool:
    return all(term in haystack.lower() for term in query.lower().split())


def _note_fact_counts(snap: Snapshot) -> Counter[str]:
    counts: Counter[str] = Counter()
    for fact in snap.facts:
        for note_id in {snap.episode_note.get(e) for e in fact.episodes} - {None}:
            counts[note_id] += 1  # type: ignore[index]
    return counts


def fact_dict(snap: Snapshot, fact: Fact, with_sources: bool = True) -> dict[str, Any]:
    notes = snap.fact_notes(fact)
    categories: list[str] = []
    agents: list[str] = []
    for note in notes:
        for category in note.categories:
            if category not in categories:
                categories.append(category)
        if note.agent and note.agent not in agents:
            agents.append(note.agent)
    data = {
        'uuid': fact.uuid,
        'fact': fact.fact,
        'relation': fact.relation,
        'status': snap.status(fact),
        'subject': {'uuid': fact.source_uuid, 'name': fact.source_name},
        'object': {'uuid': fact.target_uuid, 'name': fact.target_name},
        'valid_at': iso(fact.valid_at),
        'invalid_at': iso(fact.invalid_at),
        'recorded_at': iso(fact.created_at),
        'categories': categories,
        'agents': agents,
        'flags': [f.as_dict() for f in scan(fact.fact)],
    }
    if with_sources:
        data['sources'] = [
            {'note_id': n.note_id, 'title': n.title, 'recorded_at': iso(n.recorded_at)}
            for n in notes
        ]
    return data


def note_dict(
    snap: Snapshot, note: Note, fact_counts: Counter[str], full: bool = False
) -> dict[str, Any]:
    content = note.content
    data = {
        'note_id': note.note_id,
        'title': note.title,
        'categories': note.categories,
        'agent': note.agent,
        'recorded_at': iso(note.recorded_at),
        'parts': len(note.parts),
        'facts': fact_counts.get(note.note_id, 0),
        'excerpt': re.sub(r'\s+', ' ', content)[:EXCERPT],
        'flags': [f.as_dict() for f in scan(content)],
    }
    if full:
        data['content'] = content
    return data


def stats(snap: Snapshot) -> dict[str, Any]:
    statuses = Counter(snap.status(f) for f in snap.facts)
    categories: Counter[str] = Counter()
    agents: Counter[str] = Counter()
    for note in snap.notes.values():
        categories.update(note.categories)
        if note.agent:
            agents[note.agent] += 1
    return {
        'notes': len(snap.notes),
        'episodes': len(snap.episodes),
        'entities': len(snap.entities),
        'facts': {
            'total': len(snap.facts),
            'current': statuses.get('current', 0),
            'superseded': statuses.get('superseded', 0),
            'not_yet_valid': statuses.get('not_yet_valid', 0),
        },
        'categories': dict(categories.most_common()),
        'agents': dict(agents.most_common()),
        'generated_at': iso(snap.now),
    }


def list_notes(
    snap: Snapshot,
    query: str = '',
    category: str = '',
    agent: str = '',
    days: int = 0,
    page: int = 1,
) -> dict[str, Any]:
    counts = _note_fact_counts(snap)
    cutoff = snap.now - timedelta(days=days) if days else None
    selected = []
    for note in snap.notes.values():
        if category and category not in note.categories:
            continue
        if agent and (note.agent or '') != agent:
            continue
        if cutoff and (not note.recorded_at or note.recorded_at < cutoff):
            continue
        if query and not _contains(f'{note.title}\n{note.content}', query):
            continue
        selected.append(note)
    selected.sort(key=lambda n: n.recorded_at.timestamp() if n.recorded_at else 0, reverse=True)
    result = _page(selected, page)
    result['items'] = [note_dict(snap, n, counts) for n in result['items']]
    return result


def note_detail(snap: Snapshot, note_id: str) -> dict[str, Any] | None:
    note = snap.notes.get(note_id)
    if note is None:
        return None
    uuids = set(note.episode_uuids)
    facts = [f for f in snap.facts if uuids.intersection(f.episodes)]
    mentioned = {entity_uuid for ep_uuid, entity_uuid in snap.mentions if ep_uuid in uuids}
    entities = [snap.entities_by_uuid[u] for u in mentioned if u in snap.entities_by_uuid]
    data = note_dict(snap, note, _note_fact_counts(snap), full=True)
    data['facts_list'] = [fact_dict(snap, f, with_sources=False) for f in facts]
    data['entities'] = [
        {'uuid': e.uuid, 'name': e.name, 'labels': e.labels}
        for e in sorted(entities, key=lambda e: e.name.lower())
    ]
    return data


def list_facts(
    snap: Snapshot,
    status: str = '',
    query: str = '',
    category: str = '',
    agent: str = '',
    entity: str = '',
    page: int = 1,
) -> dict[str, Any]:
    selected = []
    for fact in snap.facts:
        current = snap.status(fact)
        if status and status != 'all' and current != status:
            continue
        if not status and current == 'superseded':
            continue  # default view hides superseded facts
        if entity and entity not in (fact.source_uuid, fact.target_uuid):
            continue
        if query and not _contains(f'{fact.fact} {fact.source_name} {fact.target_name}', query):
            continue
        data = fact_dict(snap, fact)
        if category and category not in data['categories']:
            continue
        if agent and agent not in data['agents']:
            continue
        selected.append(data)
    selected.sort(key=lambda d: d['recorded_at'] or '', reverse=True)
    return _page(selected, page)


def list_entities(snap: Snapshot, query: str = '', page: int = 1) -> dict[str, Any]:
    degree: Counter[str] = Counter()
    for fact in snap.facts:
        degree[fact.source_uuid] += 1
        degree[fact.target_uuid] += 1
    rows = [
        {
            'uuid': e.uuid,
            'name': e.name,
            'labels': e.labels,
            'summary': e.summary,
            'facts': degree.get(e.uuid, 0),
        }
        for e in snap.entities
        if not query or _contains(f'{e.name} {e.summary}', query)
    ]
    rows.sort(key=lambda r: (-r['facts'], r['name'].lower()))
    return _page(rows, page)


def entity_detail(snap: Snapshot, entity_uuid: str) -> dict[str, Any] | None:
    entity = snap.entities_by_uuid.get(entity_uuid)
    if entity is None:
        return None
    facts = [f for f in snap.facts if entity_uuid in (f.source_uuid, f.target_uuid)]
    facts.sort(key=lambda f: f.created_at.timestamp() if f.created_at else 0, reverse=True)
    return {
        'uuid': entity.uuid,
        'name': entity.name,
        'labels': entity.labels,
        'summary': entity.summary,
        'recorded_at': iso(entity.created_at),
        'facts_list': [fact_dict(snap, f) for f in facts],
    }


def graph_view(
    snap: Snapshot,
    center: str = '',
    hops: int = 1,
    statuses: set[str] | None = None,
    category: str = '',
    query: str = '',
    limit: int = 80,
) -> dict[str, Any]:
    """A bounded sub-graph: around one entity, around a search hit, or the busiest entities."""
    wanted = statuses or {'current', 'not_yet_valid'}
    edges: list[tuple[Fact, dict[str, Any]]] = []
    for fact in snap.facts:
        data = fact_dict(snap, fact, with_sources=False)
        if data['status'] not in wanted:
            continue
        if category and category not in data['categories']:
            continue
        edges.append((fact, data))

    adjacency: dict[str, set[str]] = defaultdict(set)
    degree: Counter[str] = Counter()
    for fact, _ in edges:
        adjacency[fact.source_uuid].add(fact.target_uuid)
        adjacency[fact.target_uuid].add(fact.source_uuid)
        degree[fact.source_uuid] += 1
        degree[fact.target_uuid] += 1

    seeds: list[str] = []
    if center and center in snap.entities_by_uuid:
        seeds = [center]
    elif query:
        seeds = [e.uuid for e in snap.entities if _contains(e.name, query)]

    if seeds:
        reached = set(seeds)
        queue = deque((s, 0) for s in seeds)
        while queue:
            node, depth = queue.popleft()
            if depth >= max(1, min(hops, 4)):
                continue
            for neighbour in adjacency.get(node, ()):
                if neighbour not in reached:
                    reached.add(neighbour)
                    queue.append((neighbour, depth + 1))
        ranked = sorted(reached, key=lambda u: (u not in seeds, -degree[u]))
    else:
        ranked = [u for u, _ in degree.most_common()]

    total_nodes = len(ranked)
    keep = set(ranked[:limit])
    nodes = []
    for uuid in keep:
        entity: Entity | None = snap.entities_by_uuid.get(uuid)
        if entity is None:
            continue
        nodes.append(
            {
                'id': uuid,
                'label': entity.name,
                'labels': entity.labels,
                'summary': entity.summary,
                'degree': degree.get(uuid, 0),
                'seed': uuid in seeds,
            }
        )
    out_edges = [
        {
            'id': fact.uuid,
            'source': fact.source_uuid,
            'target': fact.target_uuid,
            'label': fact.relation.replace('_', ' ').lower(),
            'fact': fact.fact,
            'status': data['status'],
        }
        for fact, data in edges
        if fact.source_uuid in keep and fact.target_uuid in keep
    ]
    return {
        'nodes': nodes,
        'edges': out_edges,
        'total_nodes': total_nodes,
        'truncated': total_nodes > len(keep),
    }


def review(snap: Snapshot, days: int = 7) -> dict[str, Any]:
    """What was added recently, grouped by agent, plus advisory problems worth a look."""
    counts = _note_fact_counts(snap)
    cutoff = snap.now - timedelta(days=days)
    recent = [n for n in snap.notes.values() if n.recorded_at and n.recorded_at >= cutoff]
    recent.sort(key=lambda n: n.recorded_at.timestamp(), reverse=True)  # type: ignore[union-attr]

    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for note in recent:
        by_agent[note.agent or 'unknown'].append(note_dict(snap, note, counts))

    sensitive_notes = [
        note_dict(snap, n, counts) for n in snap.notes.values() if scan(n.content)
    ]
    sensitive_facts = [
        fact_dict(snap, f) for f in snap.facts if scan(f.fact)
    ]

    normalized: dict[str, list[Fact]] = defaultdict(list)
    for fact in snap.facts:
        if snap.status(fact) == 'current':
            normalized[re.sub(r'\W+', ' ', fact.fact.lower()).strip()].append(fact)
    duplicates = [
        [fact_dict(snap, f) for f in group] for group in normalized.values() if len(group) > 1
    ]

    no_source = [
        fact_dict(snap, f)
        for f in snap.facts
        if not snap.fact_notes(f) and snap.status(f) != 'superseded'
    ]
    empty_notes = [note_dict(snap, n, counts) for n in snap.notes.values() if counts[n.note_id] == 0]

    return {
        'days': days,
        'recent_by_agent': dict(by_agent),
        'recent_count': len(recent),
        'sensitive_notes': sensitive_notes,
        'sensitive_facts': sensitive_facts,
        'duplicate_facts': duplicates,
        'facts_without_source': no_source,
        'notes_without_facts': empty_notes,
    }


def categories_in_use(snap: Snapshot) -> list[str]:
    seen: set[str] = set()
    for episode in snap.episodes:
        seen.update(parse_categories(episode.source_description))
    return sorted(seen)


def agents_in_use(snap: Snapshot) -> list[str]:
    return sorted({a for e in snap.episodes if (a := parse_agent(e.source_description))})


def export(snap: Snapshot) -> dict[str, Any]:
    counts = _note_fact_counts(snap)
    return {
        'exported_at': iso(snap.now),
        'notes': [note_dict(snap, n, counts, full=True) for n in snap.notes.values()],
        'facts': [fact_dict(snap, f) for f in snap.facts],
        'entities': [
            {'uuid': e.uuid, 'name': e.name, 'labels': e.labels, 'summary': e.summary}
            for e in snap.entities
        ],
    }
