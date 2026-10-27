from __future__ import annotations

from datetime import timedelta

from app import domain, ops
from app.parsing import fact_status, parse_agent, parse_categories, parse_dt, parse_note_ref
from app.sensitive import scan

from .conftest import NOW


class TestParsing:
    def test_tags(self):
        src = 'agent=claude; categories=Finance,Health Fitness; note=abc; part=2/5'
        assert parse_agent(src) == 'claude'
        assert parse_categories(src) == ['finance', 'health_fitness']
        ref = parse_note_ref(src)
        assert (ref.note_id, ref.part, ref.parts) == ('abc', 2, 5)

    def test_untagged_source(self):
        assert parse_note_ref('some other source') is None
        assert parse_agent(None) is None and parse_categories('') == []

    def test_dates(self):
        assert parse_dt('2026-01-01T00:00:00').tzinfo is not None
        assert parse_dt('not a date') is None and parse_dt(None) is None

    def test_fact_status_matches_server_rules(self):
        past, future = NOW - timedelta(days=1), NOW + timedelta(days=1)
        assert fact_status(None, None, None, NOW) == 'current'
        assert fact_status(future, None, None, NOW) == 'not_yet_valid'
        assert fact_status(None, past, None, NOW) == 'superseded'
        assert fact_status(None, future, None, NOW) == 'current'
        assert fact_status(None, None, past, NOW) == 'superseded'


class TestSensitive:
    def test_flags_without_leaking_the_value(self):
        flags = scan('card 4111 1111 1111 1111 and password is hunter2xyz')
        kinds = {f.kind for f in flags}
        assert {'card number', 'password or PIN'} <= kinds
        assert all('4111' not in f.hint and 'hunter2xyz' not in f.hint for f in flags)

    def test_other_kinds(self):
        assert any(f.kind == 'one-time code' for f in scan('Your verification code is 482913'))
        assert any(f.kind == 'API key or token' for f in scan('key sk-abcdefghijklmnopqrstuv'))
        assert any(f.kind == 'bank account details' for f in scan('sort code 20-00-00 account 12345678'))
        assert any(f.kind == 'National Insurance number' for f in scan('NI number AB 12 34 56 C'))
        assert not scan('Reference QQ 12 34 56 C')  # QQ is not a valid NI prefix

    def test_placeholders_and_variable_references_are_not_flagged(self):
        assert scan('credential helper that supplies username=x and password=$BITBUCKET_TOKEN') == []
        assert scan('The password is <your-password> or password: ******') == []
        assert scan('password: ${DB_PASS}') == []
        assert [f.kind for f in scan('password=hunter2xyz')] == ['password or PIN']

    def test_clean_text_and_non_luhn_numbers(self):
        assert scan('My mortgage lender is Halifax and I was born in 1980.') == []
        assert scan('Order number 1234 5678 9012 3456') == []
        assert scan(None) == []


class TestSnapshot:
    def test_parts_group_into_notes(self, snap):
        assert set(snap.notes) == {'n1', 'n2', 'n3'}
        assert [p.uuid for p in snap.notes['n2'].parts] == ['e2a', 'e2b']
        assert snap.notes['n2'].content.endswith('Hotel Alfama.')
        assert snap.notes['n2'].agent == 'grok'

    def test_statuses(self, snap):
        got = {f.uuid: snap.status(f) for f in snap.facts}
        assert got == {
            'f1': 'current', 'f2': 'superseded', 'f3': 'not_yet_valid',
            'f4': 'current', 'f5': 'current',
        }


class TestViews:
    def test_stats(self, snap):
        data = domain.stats(snap)
        assert data['notes'] == 3 and data['episodes'] == 4
        assert data['facts'] == {'total': 5, 'current': 3, 'superseded': 1, 'not_yet_valid': 1}
        assert data['categories']['travel'] == 1

    def test_notes_filters_and_order(self, snap):
        all_notes = domain.list_notes(snap)
        assert [n['note_id'] for n in all_notes['items']] == ['n3', 'n2', 'n1']
        assert domain.list_notes(snap, category='travel')['total'] == 1
        assert domain.list_notes(snap, agent='claude')['items'][0]['note_id'] == 'n1'
        assert domain.list_notes(snap, query='alfama')['items'][0]['note_id'] == 'n2'
        assert domain.list_notes(snap, days=1)['total'] == 2  # n1 is two days old

    def test_note_detail(self, snap):
        data = domain.note_detail(snap, 'n2')
        assert data['parts'] == 2 and 'Lisbon' in data['content']
        assert {f['uuid'] for f in data['facts_list']} == {'f3', 'f4', 'f5'}
        assert {e['name'] for e in data['entities']} == {'Gary Stimson', 'Lisbon', 'Hotel Alfama'}
        assert domain.note_detail(snap, 'missing') is None

    def test_facts_default_hides_superseded(self, snap):
        default = {f['uuid'] for f in domain.list_facts(snap)['items']}
        assert default == {'f1', 'f3', 'f4', 'f5'}
        assert {f['uuid'] for f in domain.list_facts(snap, status='all')['items']} >= {'f2'}
        only = domain.list_facts(snap, status='superseded')['items']
        assert [f['uuid'] for f in only] == ['f2']
        assert domain.list_facts(snap, entity='alfama')['total'] == 1
        assert domain.list_facts(snap, category='finance')['total'] == 2

    def test_entities_sorted_by_facts(self, snap):
        names = [e['name'] for e in domain.list_entities(snap)['items']]
        assert names[0] == 'Gary Stimson' and names[-1] == 'Unused'

    def test_graph_view(self, snap):
        full = domain.graph_view(snap)
        ids = {n['id'] for n in full['nodes']}
        assert 'lonely' not in ids and {'gary', 'lisbon'} <= ids
        assert {e['id'] for e in full['edges']} == {'f1', 'f3', 'f4', 'f5'}  # no superseded
        history = domain.graph_view(snap, statuses={'superseded'})
        assert [e['id'] for e in history['edges']] == ['f2']
        around = domain.graph_view(snap, center='alfama', hops=1)
        assert {n['id'] for n in around['nodes']} == {'alfama', 'gary'}
        by_query = domain.graph_view(snap, query='halifax')
        assert any(n['seed'] for n in by_query['nodes'])
        small = domain.graph_view(snap, limit=10)
        assert small['truncated'] is False

    def test_review_flags(self, snap):
        data = domain.review(snap, days=1)
        assert data['recent_count'] == 2  # n2 and n3 (n1 is 2 days old)
        assert [n['note_id'] for n in data['sensitive_notes']] == ['n3']
        assert {n['note_id'] for n in data['notes_without_facts']} == {'n3'}
        assert set(data['recent_by_agent']) == {'grok', 'openmaus'}


class TestDeletePlans:
    def test_note_delete_keeps_shared_facts_and_finds_orphans(self, snap):
        plan = ops.plan_note_delete(snap, snap.notes['n2'])
        assert {f['uuid'] for f in plan['facts_deleted']} == {'f3', 'f4'}
        assert {f['uuid'] for f in plan['facts_kept']} == {'f5'}  # also supported by e1
        # Hotel Alfama only appeared in this note; Lisbon still has f5; Gary has more
        assert [e['name'] for e in plan['entities_deleted']] == ['Hotel Alfama']
        assert '_deleted' not in ops.public_plan(plan)

    def test_fact_delete_plan(self, snap):
        plan = ops.plan_fact_delete(snap, snap.facts_by_uuid['f4'])
        assert plan['entities_deleted'] == []  # Hotel Alfama still mentioned by e2b


class RecordingAudit(ops.Audit):
    pass


class TestOps:
    def test_delete_note_queries_and_audit(self, snap, tmp_path):
        from .conftest import FakeStore

        store, audit = FakeStore(snap), ops.Audit(tmp_path)
        result = ops.delete_note(store, audit, snap, snap.notes['n2'])
        sql = '\n'.join(q for q, _ in store.queries)
        assert 'DELETE r' in sql and 'DETACH DELETE e' in sql and 'DETACH DELETE n' in sql
        trimmed = [p for q, p in store.queries if 'SET r.episodes' in q]
        assert trimmed == [{'u': 'f5', 'eps': ['e1']}]
        assert result['change']['action'] == 'delete_note'
        assert store.invalidations == 1
        assert audit.entries()[0]['id'] == result['change']['id']
        assert list(audit.trash_dir.glob('*.json'))  # recoverable snapshot written

    def test_end_and_restore(self, snap, tmp_path):
        from .conftest import FakeStore

        store, audit = FakeStore(snap), ops.Audit(tmp_path)
        ops.end_fact(store, audit, snap.facts_by_uuid['f1'])
        ops.restore_fact(store, audit, snap.facts_by_uuid['f2'])
        assert 'SET r.invalid_at = $at' in store.queries[0][0]
        assert 'r.invalid_at = null' in store.queries[1][0]
        assert [e['action'] for e in audit.entries()] == ['restore_fact', 'end_fact']
