"""Unit tests for notes: splitting, note identity, excerpts and recall sources."""

from datetime import datetime, timezone

from graphiti_core.edges import EntityEdge

from services.category_service import encode_source_description, parse_categories
from services.notes_service import (
    NoteRef,
    assemble_note,
    derive_title,
    excerpt,
    note_tags,
    parse_note_ref,
    part_uuid,
    rank_notes,
    split_note,
)
from services.recall_service import EpisodeTags, sources_for_edge

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def paragraph(word: str, sentences: int = 8) -> str:
    return ' '.join(
        f'{word.capitalize()} sentence number {i} has some detail.' for i in range(sentences)
    )


class TestSplitNote:
    def test_short_note_is_one_part(self):
        parts = split_note('# Pension\n\nI pay 5% into my pension.', max_chars=2000)
        assert len(parts) == 1
        assert parts[0].heading == 'Pension'

    def test_long_note_splits_at_headings_and_reassembles(self):
        note = '\n\n'.join(
            [
                '# Finances 2026',
                paragraph('savings'),
                '## Mortgage',
                paragraph('mortgage'),
                paragraph('rates'),
                '## Pension',
                paragraph('pension'),
            ]
        )
        parts = split_note(note, max_chars=800)

        assert len(parts) > 1
        assert all(len(part.text) <= 800 for part in parts)
        assert assemble_note([(i, p.text) for i, p in enumerate(parts)]) == note
        for part in parts:
            blocks = part.text.split('\n\n')
            assert not blocks[-1].startswith('#'), 'heading separated from its content'
            if blocks[0].startswith('## '):
                assert part.heading == blocks[0][3:]

    def test_heading_is_carried_into_the_next_part(self):
        note = '\n\n'.join([paragraph('intro', 9), '## Pension', paragraph('pension', 9)])
        parts = split_note(note, max_chars=500)

        assert [p.heading for p in parts] == [None, 'Pension']
        assert parts[1].text.startswith('## Pension')

    def test_oversized_paragraph_splits_at_sentences(self):
        parts = split_note(paragraph('long', sentences=60), max_chars=500)

        assert len(parts) > 1
        assert all(len(part.text) <= 500 for part in parts)
        assert all(part.text.endswith('.') for part in parts)

    def test_unbroken_text_is_hard_wrapped(self):
        parts = split_note('x' * 1200, max_chars=500)
        assert [len(p.text) for p in parts] == [500, 500, 200]


class TestNoteIdentity:
    def test_tags_round_trip_through_source_description(self):
        ref = NoteRef(note_id='n-1', part=2, parts=3)
        description = encode_source_description(['finance'], agent='grok', extra=note_tags(ref))

        assert parse_note_ref(description) == ref
        assert parse_categories(description) == ['finance']
        assert parse_note_ref(encode_source_description(['finance'])) is None

    def test_part_uuids_are_deterministic_and_distinct(self):
        assert part_uuid('n-1', 1) == part_uuid('n-1', 1)
        assert part_uuid('n-1', 1) != part_uuid('n-1', 2)
        assert part_uuid('n-1', 1) != part_uuid('n-2', 1)

    def test_derive_title(self):
        assert derive_title('## Mortgage plan\n\nDetails') == 'Mortgage plan'
        assert derive_title('My salary went up.\nMore') == 'My salary went up.'
        assert derive_title('a' * 100, max_chars=10) == 'a' * 9 + '…'
        assert derive_title('   ') is None


def test_excerpt_picks_best_matching_sentence():
    content = 'I moved house in May. The new mortgage is with Halifax at 4.1%. I like the area.'
    assert excerpt(content, 'Gary has a mortgage with Halifax') == (
        'The new mortgage is with Halifax at 4.1%.'
    )
    assert excerpt(content, 'unrelated words entirely') == 'I moved house in May.'
    assert excerpt('word ' * 100, 'word', max_chars=20).endswith('…')


def test_rank_notes_rewards_appearing_in_both_lists():
    assert rank_notes([['a', 'b'], ['b', 'c']])[0] == 'b'


def test_sources_for_edge_dedupes_parts_of_one_note_and_orders_newest_first():
    edge = EntityEdge(
        uuid='f',
        group_id='personal',
        source_node_uuid='s',
        target_node_uuid='t',
        created_at=NOW,
        name='HAS',
        fact='Owner has a mortgage with Halifax',
        episodes=['p1', 'p2', 'old', 'raw'],
    )
    tags = {
        'p1': EpisodeTags(
            note_id='n',
            title='Plan',
            recorded_at='2026-10-05',
            content='Mortgage with Halifax.',
            agents=['claude'],
        ),
        'p2': EpisodeTags(
            note_id='n', title='Plan', recorded_at='2026-10-05', content='Other part.'
        ),
        'old': EpisodeTags(
            note_id='m', title='Old', recorded_at='2026-01-01', content='Nationwide.'
        ),
        'raw': EpisodeTags(),
    }

    sources = sources_for_edge(edge, tags)

    assert [s['note_id'] for s in sources] == ['n', 'm']
    assert sources[0]['excerpt'] == 'Mortgage with Halifax.'
    assert sources[0]['agent'] == 'claude'
