"""Unit tests for the personal-memory layer: categories, recall selection and access."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from graphiti_core.edges import EntityEdge

from config.schema import CategoryConfig, GraphitiConfig, ServerConfig
from services.category_service import (
    GENERAL_CATEGORY,
    CategoryTaxonomy,
    encode_source_description,
    parse_agent,
    parse_categories,
)
from services.recall_service import (
    QUERY_SOURCE,
    EpisodeTags,
    fact_status,
    select_facts,
    sort_history,
)
from utils.access import build_transport_security, mask_secret_path, mcp_endpoint_path

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
SECRET = 'a' * 40

TAXONOMY = CategoryTaxonomy(
    [
        CategoryConfig(
            name='finance',
            description='Money',
            keywords=['bank', 'savings', 'credit card'],
            related=['employment', 'property'],
        ),
        CategoryConfig(
            name='property',
            description='Home',
            keywords=['mortgage', 'rent'],
            related=['finance', 'employment'],
        ),
        CategoryConfig(name='employment', description='Work', keywords=['salary', 'job']),
        CategoryConfig(name='Health Fitness', description='Gym', keywords=['gym']),
    ]
)


def make_edge(uuid: str, episodes: list[str], **times) -> EntityEdge:
    return EntityEdge(
        uuid=uuid,
        group_id='personal',
        source_node_uuid='s',
        target_node_uuid='t',
        created_at=times.pop('created_at', NOW - timedelta(days=30)),
        name='RELATES_TO',
        fact=f'fact {uuid}',
        episodes=episodes,
        **times,
    )


class TestCategoryTaxonomy:
    def test_infers_categories_from_whole_word_keywords(self):
        assert TAXONOMY.infer('Thinking about my mortgages') == ['property']
        assert TAXONOMY.infer('My salary went up and I moved bank') == ['finance', 'employment']
        assert TAXONOMY.infer('The current situation') == []  # "rent" inside "current"

    def test_category_name_matches_as_keyword(self):
        assert TAXONOMY.infer('health fitness plan') == ['health_fitness']

    def test_resolve_prefers_explicit_then_inferred_then_general(self):
        assert TAXONOMY.resolve(['Finance', 'finance', 'New Thing'], 'gym') == [
            'finance',
            'new_thing',
        ]
        assert TAXONOMY.resolve(None, 'gym session') == ['health_fitness']
        assert TAXONOMY.resolve([], 'nothing relevant') == [GENERAL_CATEGORY]

    def test_expand_adds_related_categories_once(self):
        assert TAXONOMY.expand(['property']) == ['property', 'finance', 'employment']
        assert TAXONOMY.expand(['unknown']) == ['unknown']

    def test_source_description_round_trip(self):
        description = encode_source_description(['finance', 'property'], agent='claude; x')
        assert parse_categories(description) == ['finance', 'property']
        assert parse_agent(description) == 'claude, x'
        assert parse_categories('plain upstream description') == []
        assert parse_agent(None) is None


class TestFactStatus:
    def test_current_superseded_and_future(self):
        assert fact_status(make_edge('a', [], valid_at=NOW - timedelta(days=1)), NOW) == 'current'
        assert (
            fact_status(make_edge('b', [], invalid_at=NOW - timedelta(days=1)), NOW) == 'superseded'
        )
        assert fact_status(make_edge('c', [], invalid_at=NOW + timedelta(days=1)), NOW) == 'current'
        assert (
            fact_status(make_edge('d', [], valid_at=NOW + timedelta(days=1)), NOW)
            == 'not_yet_valid'
        )

    def test_expired_without_end_date_is_superseded(self):
        edge = make_edge('e', [], expired_at=NOW - timedelta(hours=1))
        assert fact_status(edge, NOW) == 'superseded'
        assert fact_status(edge, NOW - timedelta(days=1)) == 'current'

    def test_naive_datetimes_are_treated_as_utc(self):
        edge = make_edge('f', [], invalid_at=(NOW - timedelta(days=1)).replace(tzinfo=None))
        assert fact_status(edge, NOW) == 'superseded'


class TestSelectFacts:
    tags = {
        'ep-property': EpisodeTags(categories=['property'], agents=['claude']),
        'ep-finance': EpisodeTags(categories=['finance'], agents=['grok']),
        'ep-gym': EpisodeTags(categories=['health_fitness'], agents=['openmaus']),
    }

    def select(self, hits, categories, strict=False, include_history=False, limit=10):
        return select_facts(hits, self.tags, categories, strict, include_history, NOW, limit)

    def test_superseded_facts_hidden_unless_history_requested(self):
        old = make_edge('old', ['ep-property'], invalid_at=NOW - timedelta(days=14))
        new = make_edge('new', ['ep-property'])
        hits = [(old, QUERY_SOURCE), (new, QUERY_SOURCE)]

        assert [f['uuid'] for f in self.select(hits, ['property'])] == ['new']
        history = self.select(hits, ['property'], include_history=True)
        assert [(f['uuid'], f['status']) for f in history] == [
            ('old', 'superseded'),
            ('new', 'current'),
        ]

    def test_upcoming_facts_kept_only_when_requested(self):
        old = make_edge('old', ['ep-property'], invalid_at=NOW - timedelta(days=14))
        soon = make_edge('soon', ['ep-property'], valid_at=NOW + timedelta(days=90))
        hits = [(old, QUERY_SOURCE), (soon, QUERY_SOURCE)]

        assert self.select(hits, ['property']) == []
        upcoming = select_facts(hits, self.tags, ['property'], False, False, NOW, 10, True)
        assert [(f['uuid'], f['status']) for f in upcoming] == [('soon', 'not_yet_valid')]

    def test_category_hits_must_carry_their_category(self):
        finance = make_edge('fin', ['ep-finance'])
        gym = make_edge('gym', ['ep-gym'])
        untagged = make_edge('raw', ['ep-unknown'])
        hits = [(finance, 'finance'), (gym, 'finance'), (untagged, 'finance')]

        assert [f['uuid'] for f in self.select(hits, ['property', 'finance'])] == ['fin', 'raw']

    def test_query_hits_kept_unless_strict(self):
        gym = make_edge('gym', ['ep-gym'])
        assert [f['uuid'] for f in self.select([(gym, QUERY_SOURCE)], ['finance'])] == ['gym']
        assert self.select([(gym, QUERY_SOURCE)], ['finance'], strict=True) == []

    def test_duplicates_merge_matched_sources_and_respect_limit(self):
        first = make_edge('one', ['ep-property', 'ep-finance'])
        second = make_edge('two', ['ep-finance'])
        hits = [(first, QUERY_SOURCE), (first, 'finance'), (second, 'finance')]

        facts = self.select(hits, ['property', 'finance'], limit=1)
        assert len(facts) == 1
        assert facts[0]['matched'] == [QUERY_SOURCE, 'finance']
        assert facts[0]['categories'] == ['property', 'finance']
        assert facts[0]['agents'] == ['claude', 'grok']

    def test_category_hit_without_that_category_does_not_add_a_match(self):
        fact = make_edge('prop', ['ep-property'])
        hits = [(fact, QUERY_SOURCE), (fact, 'finance'), (fact, 'property')]

        facts = self.select(hits, ['property', 'finance'])
        assert facts[0]['matched'] == [QUERY_SOURCE, 'property']

    def test_sort_history_orders_by_valid_at(self):
        facts = [
            {'valid_at': '2026-09-01', 'recorded_at': 'x'},
            {'valid_at': None, 'recorded_at': 'y'},
            {'valid_at': '2026-01-01', 'recorded_at': 'z'},
        ]
        assert [f['recorded_at'] for f in sort_history(facts)] == ['y', 'z', 'x']


class TestAccess:
    def test_endpoint_path_with_and_without_secret(self):
        assert mcp_endpoint_path(None) == '/mcp'
        assert mcp_endpoint_path(SECRET) == f'/{SECRET}/mcp'

    @pytest.mark.parametrize('secret', ['short', 'x' * 31, 'a/b' + 'c' * 40, 'a b' + 'c' * 40])
    def test_rejects_weak_or_unsafe_secrets(self, secret):
        with pytest.raises(ValueError):
            mcp_endpoint_path(secret)

    def test_secret_is_masked_in_logs(self):
        masked = mask_secret_path(f'/{SECRET}/mcp', SECRET)
        assert SECRET not in masked
        assert masked.startswith('/aaaa')

    def test_transport_security_for_allowed_hosts(self):
        assert build_transport_security([]) is None
        settings = build_transport_security(['home.tail1.ts.net'])
        assert settings is not None
        assert settings.enable_dns_rebinding_protection
        assert 'home.tail1.ts.net' in settings.allowed_hosts
        assert 'https://home.tail1.ts.net' in settings.allowed_origins
        # Funnel on port 10000 sends the port in the Host header
        assert 'home.tail1.ts.net:*' in settings.allowed_hosts
        assert 'https://home.tail1.ts.net:*' in settings.allowed_origins

    def test_allowed_hosts_accepts_comma_separated_string(self):
        assert ServerConfig(allowed_hosts='a.ts.net, b.ts.net,').allowed_hosts == [  # type: ignore[arg-type]
            'a.ts.net',
            'b.ts.net',
        ]
        assert ServerConfig(allowed_hosts=None).allowed_hosts == []  # type: ignore[arg-type]


def test_home_config_loads(monkeypatch):
    config_path = Path(__file__).parent.parent / 'config' / 'config-home.yaml'
    monkeypatch.setenv('CONFIG_PATH', str(config_path))
    monkeypatch.setenv('MCP_SECRET_PATH', SECRET)
    monkeypatch.setenv('MCP_ALLOWED_HOSTS', 'home.tail1.ts.net')
    monkeypatch.setenv('MEMORY_OWNER_NAME', 'Test Owner')

    config = GraphitiConfig()

    assert config.server.secret_path == SECRET
    assert config.server.allowed_hosts == ['home.tail1.ts.net']
    assert config.graphiti.owner_name == 'Test Owner'
    taxonomy = CategoryTaxonomy(config.graphiti.categories)
    assert taxonomy.expand(taxonomy.infer('Should I remortgage?')) == [
        'property',
        'finance',
        'employment',
    ]
    for category in taxonomy.categories.values():
        assert set(taxonomy.normalize(category.related)) <= set(taxonomy.categories)
    assert yaml.safe_load(config_path.read_text())['graphiti']['group_id']
