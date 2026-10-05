"""Notes: the full text agents store with ``remember``.

A note is stored as one or more Graphiti episodes. Long notes are split at headings
and paragraphs into parts so each part gets a thorough fact extraction. Every part
carries ``note=<id>`` and ``part=<i>/<n>`` tags in its source_description, and part
UUIDs are derived from the note ID, so a note can be reassembled from any of its
parts without a custom query.

This module is free of I/O so it can be unit-tested without a database or LLM.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_PART_CHARS = 2000
EXCERPT_CHARS = 240

_NOTE_NAMESPACE = uuid.UUID('6f1d7c62-3a0b-4c55-9a43-7e0d2f6b9c11')
_NOTE_PATTERN = re.compile(r'(?:^|;\s*)note=([^;\s]+)')
_PART_PATTERN = re.compile(r'(?:^|;\s*)part=(\d+)/(\d+)')
_HEADING_PATTERN = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$')
_SENTENCE_PATTERN = re.compile(r'(?<=[.!?])\s+(?=\S)')
_WORD_PATTERN = re.compile(r'[a-z0-9£$€%.]+')


@dataclass
class NoteRef:
    """Where an episode sits within its note."""

    note_id: str
    part: int
    parts: int


@dataclass
class NotePart:
    """One part of a split note and the heading it falls under."""

    text: str
    heading: str | None


def new_note_id() -> str:
    return str(uuid.uuid4())


def part_uuid(note_id: str, part: int) -> str:
    """Deterministic episode UUID for a part (1-based) of a note."""
    return str(uuid.uuid5(_NOTE_NAMESPACE, f'{note_id}:{part}'))


def note_tags(ref: NoteRef) -> dict[str, str]:
    """Extra source_description tags identifying the note part."""
    return {'note': ref.note_id, 'part': f'{ref.part}/{ref.parts}'}


def parse_note_ref(source_description: str | None) -> NoteRef | None:
    """Read the tags written by note_tags."""
    if not source_description:
        return None
    note = _NOTE_PATTERN.search(source_description)
    if not note:
        return None
    part = _PART_PATTERN.search(source_description)
    if not part:
        return NoteRef(note_id=note.group(1), part=1, parts=1)
    return NoteRef(note_id=note.group(1), part=int(part.group(1)), parts=int(part.group(2)))


def _heading(block: str) -> str | None:
    first_line = block.split('\n', 1)[0]
    match = _HEADING_PATTERN.match(first_line)
    return match.group(1) if match else None


def _split_long_block(block: str, max_chars: int) -> list[str]:
    """Split an oversized paragraph at sentence boundaries (hard-wrap as a last resort)."""
    pieces: list[str] = []
    current = ''
    for sentence in _SENTENCE_PATTERN.split(block):
        while len(sentence) > max_chars:
            if current:
                pieces.append(current)
                current = ''
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        candidate = f'{current} {sentence}' if current else sentence
        if len(candidate) > max_chars and current:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def split_note(content: str, max_chars: int = DEFAULT_PART_CHARS) -> list[NotePart]:
    """Split a note into parts of at most max_chars, preferring headings, then paragraphs.

    Parts are whole paragraphs where possible, so joining the parts with blank lines
    reproduces the note. A new heading starts a new part once the current part is at
    least half full.
    """
    text = content.replace('\r\n', '\n').strip()
    if len(text) <= max_chars:
        return [NotePart(text=text, heading=_heading(text))]

    blocks = [block.strip() for block in re.split(r'\n\s*\n', text) if block.strip()]
    parts: list[NotePart] = []
    current: list[str] = []
    current_len = 0
    section: str | None = None
    current_heading: str | None = None

    def flush() -> None:
        nonlocal current, current_len
        if current:
            parts.append(NotePart(text='\n\n'.join(current), heading=current_heading))
        current, current_len = [], 0

    for block in blocks:
        heading = _heading(block)
        if heading is not None:
            section = heading
            if current_len >= max_chars // 2:
                flush()
        pieces = [block] if len(block) <= max_chars else _split_long_block(block, max_chars)
        for piece in pieces:
            added = len(piece) + (2 if current else 0)
            if current and current_len + added > max_chars:
                # Keep a trailing heading with the content it introduces.
                carried = current.pop() if len(current) > 1 and _heading(current[-1]) else None
                if carried is not None:
                    current_len -= len(carried) + 2
                flush()
                if carried is not None:
                    current, current_len = [carried], len(carried)
                    current_heading = _heading(carried)
                added = len(piece) + (2 if current else 0)
            if not current:
                current_heading = section
            current.append(piece)
            current_len += added
    flush()
    return parts


def derive_title(content: str, max_chars: int = 80) -> str | None:
    """Use the note's first heading, or its first line, as a title."""
    text = content.strip()
    if not text:
        return None
    first_line = text.split('\n', 1)[0].strip()
    title = _heading(first_line) or first_line
    if len(title) > max_chars:
        title = title[: max_chars - 1].rstrip() + '…'
    return title


def _words(text: str) -> set[str]:
    return {word.strip('.') for word in _WORD_PATTERN.findall(text.lower()) if len(word) > 2}


def excerpt(content: str, focus: str, max_chars: int = EXCERPT_CHARS) -> str:
    """The sentence of content that best matches focus, trimmed to max_chars."""
    sentences = [
        s.strip()
        for line in content.split('\n')
        for s in _SENTENCE_PATTERN.split(line)
        if s.strip()
    ]
    if not sentences:
        return ''
    focus_words = _words(focus)
    best = max(sentences, key=lambda sentence: len(focus_words & _words(sentence)))
    if not focus_words & _words(best):
        best = sentences[0]
    if len(best) > max_chars:
        best = best[: max_chars - 1].rstrip() + '…'
    return best


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def rank_notes(ranked_lists: list[list[str]]) -> list[str]:
    """Merge ranked lists of note keys with reciprocal rank fusion."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, key in enumerate(ranked):
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank + 60)
    return sorted(scores, key=lambda key: scores[key], reverse=True)


def assemble_note(parts: list[tuple[int, str]]) -> str:
    """Join note parts (part number, text) in order."""
    return '\n\n'.join(text for _, text in sorted(parts))


def note_summary(
    note_id: str,
    title: str,
    recorded_at: str | None,
    agent: str | None,
    categories: list[str],
    parts: int,
    **extra: Any,
) -> dict[str, Any]:
    """Common shape for a note in tool responses."""
    return {
        'note_id': note_id,
        'title': title,
        'recorded_at': recorded_at,
        'agent': agent,
        'categories': categories,
        'parts': parts,
        **extra,
    }
