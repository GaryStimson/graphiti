from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import Authenticator, LoginThrottle, csrf_ok
from app.main import create_app

from .conftest import PASSWORD, FakeStore

CSRF = {'X-Requested-With': 'memory-ui'}


class TestAuthHelpers:
    def test_throttle_locks_and_expires(self):
        throttle = LoginThrottle(max_failures=3, lockout_seconds=100)
        for i in range(3):
            assert throttle.retry_after('a', now=1000 + i) == 0
            throttle.record_failure('a', now=1000 + i)
        assert throttle.retry_after('a', now=1010) > 0
        assert throttle.retry_after('b', now=1010) == 0
        assert throttle.retry_after('a', now=1200) == 0  # lockout elapsed

    def test_success_clears_failures(self):
        throttle = LoginThrottle(2, 100)
        throttle.record_failure('a', now=1)
        throttle.record_success('a')
        throttle.record_failure('a', now=2)
        assert throttle.retry_after('a', now=3) == 0

    def test_session_tokens(self, settings):
        auth = Authenticator(settings)
        token = auth.issue_session()
        assert auth.session_valid(token)
        assert not auth.session_valid(token + 'x') and not auth.session_valid(None)
        other = Authenticator(settings.__class__(**{**settings.__dict__, 'data_dir': settings.data_dir / 'x'}))
        assert not other.session_valid(token)  # different signing key

    def test_password_check_and_short_password_disabled(self, settings):
        auth = Authenticator(settings)
        assert auth.password_matches(PASSWORD) and not auth.password_matches('nope')
        weak = Authenticator(settings.__class__(**{**settings.__dict__, 'ui_password': 'short'}))
        assert not weak.password_matches('short')

    def test_csrf_rules(self):
        assert csrf_ok('GET', {}, 'h')
        assert not csrf_ok('POST', {}, 'h')
        assert csrf_ok('POST', {'x-requested-with': 'memory-ui'}, 'h')
        assert not csrf_ok('POST', {'x-requested-with': 'memory-ui', 'origin': 'https://evil.com'}, 'h')
        assert csrf_ok('DELETE', {'x-requested-with': 'memory-ui', 'origin': 'http://h:8200'}, 'h:8200')


@pytest.fixture
def remembered():
    return []


@pytest.fixture
def client(settings, snap, remembered):
    async def fake_remember(**kwargs):
        remembered.append(kwargs)
        return {'note_id': 'new-note', 'message': 'queued'}

    store = FakeStore(snap)
    app = create_app(settings, store=store, remember_fn=fake_remember)  # type: ignore[arg-type]
    test_client = TestClient(app, base_url='http://testserver')
    test_client.store = store  # type: ignore[attr-defined]
    return test_client


def login(client):
    response = client.post('/auth/login', json={'password': PASSWORD}, headers=CSRF)
    assert response.status_code == 200
    return response


class TestAuthFlow:
    def test_api_requires_login(self, client):
        assert client.get('/api/stats').status_code == 401
        assert client.get('/api/export').status_code == 401
        assert client.get('/auth/me').json() == {'authenticated': False, 'configured': True}

    def test_login_cookie_flags_and_session(self, client):
        response = login(client)
        cookie = response.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie
        assert client.get('/api/stats').status_code == 200
        assert client.get('/auth/me').json()['authenticated'] is True
        client.post('/auth/logout', headers=CSRF)
        assert client.get('/api/stats').status_code == 401

    def test_wrong_password_then_lockout(self, client):
        for _ in range(5):
            assert client.post('/auth/login', json={'password': 'bad'}, headers=CSRF).status_code == 401
        blocked = client.post('/auth/login', json={'password': PASSWORD}, headers=CSRF)
        assert blocked.status_code == 429

    def test_login_needs_csrf_header(self, client):
        assert client.post('/auth/login', json={'password': PASSWORD}).status_code == 403

    def test_unconfigured_password_refuses_login(self, settings, snap):
        weak = settings.__class__(**{**settings.__dict__, 'ui_password': ''})
        app = create_app(weak, store=FakeStore(snap))  # type: ignore[arg-type]
        c = TestClient(app, base_url='http://testserver')
        assert c.post('/auth/login', json={'password': ''}, headers=CSRF).status_code == 503

    def test_unknown_host_rejected(self, client):
        assert client.get('/auth/me', headers={'Host': 'evil.example.com'}).status_code == 421
        assert client.get('/healthz', headers={'Host': 'evil.example.com'}).status_code == 200

    def test_security_headers_and_no_store(self, client):
        login(client)
        response = client.get('/api/stats')
        assert response.headers['cache-control'] == 'no-store'
        assert "default-src 'self'" in response.headers['content-security-policy']
        assert response.headers['x-frame-options'] == 'DENY'


class TestApi:
    def test_read_endpoints(self, client):
        login(client)
        assert client.get('/api/stats').json()['notes'] == 3
        assert client.get('/api/notes?category=travel').json()['total'] == 1
        assert client.get('/api/notes/n2').json()['parts'] == 2
        assert client.get('/api/notes/zzz').status_code == 404
        assert client.get('/api/facts?status=superseded').json()['total'] == 1
        assert client.get('/api/entities').json()['total'] == 5
        assert client.get('/api/entities/gary').json()['name'] == 'Gary Stimson'
        assert len(client.get('/api/graph').json()['nodes']) >= 3
        assert client.get('/api/review?days=7').json()['recent_count'] == 3
        assert client.get('/api/changes').json() == {'items': []}
        export = client.get('/api/export')
        assert 'attachment' in export.headers['content-disposition']
        assert len(export.json()['facts']) == 5

    def test_delete_plan_then_delete(self, client):
        login(client)
        plan = client.get('/api/notes/n2/delete-plan').json()
        assert {f['uuid'] for f in plan['facts_deleted']} == {'f3', 'f4'}
        assert not client.store.queries  # planning never writes
        assert client.delete('/api/notes/n2').status_code == 403  # CSRF header missing
        done = client.delete('/api/notes/n2', headers=CSRF).json()
        assert done['ok'] and client.store.queries
        assert client.get('/api/changes').json()['items'][0]['action'] == 'delete_note'

    def test_end_and_restore_fact(self, client):
        login(client)
        assert client.patch('/api/facts/f1', json={'action': 'end'}, headers=CSRF).status_code == 200
        assert client.patch('/api/facts/f2', json={'action': 'restore'}, headers=CSRF).status_code == 200
        assert client.patch('/api/facts/f1', json={'action': 'bogus'}, headers=CSRF).status_code == 422
        assert client.patch('/api/facts/nope', json={'action': 'end'}, headers=CSRF).status_code == 404
        bad_date = client.patch('/api/facts/f1', json={'action': 'end', 'at': 'x'}, headers=CSRF)
        assert bad_date.status_code == 422

    def test_rewrite_deletes_then_remembers(self, client, remembered):
        login(client)
        response = client.post(
            '/api/facts/f1/rewrite', json={'new_text': 'My mortgage lender is Nationwide.'}, headers=CSRF
        )
        assert response.status_code == 200 and response.json()['new_note'] == 'new-note'
        assert remembered == [
            {'content': 'My mortgage lender is Nationwide.', 'categories': ['finance']}
        ]
        assert any('DELETE r' in q for q, _ in client.store.queries)

    def test_rewrite_failure_is_reported(self, settings, snap):
        from app.mcp_client import RememberError

        async def failing(**kwargs):
            raise RememberError('server down')

        app = create_app(settings, store=FakeStore(snap), remember_fn=failing)  # type: ignore[arg-type]
        c = TestClient(app, base_url='http://testserver')
        login(c)
        response = c.post('/api/facts/f1/rewrite', json={'new_text': 'Corrected fact here'}, headers=CSRF)
        assert response.status_code == 502 and 'old fact was deleted' in response.json()['detail']

    def test_sensitive_text_blocked_unless_overridden(self, client, remembered):
        login(client)
        body = {'content': 'my card is 4111 1111 1111 1111'}
        blocked = client.post('/api/remember', json=body, headers=CSRF)
        assert blocked.status_code == 422 and 'card number' in blocked.json()['detail']
        assert remembered == []
        ok = client.post('/api/remember', json={**body, 'allow_sensitive': True}, headers=CSRF)
        assert ok.status_code == 200 and len(remembered) == 1

    def test_remember_proxies_to_server(self, client, remembered):
        login(client)
        response = client.post(
            '/api/remember', json={'content': 'I now live in Leeds.', 'categories': ['property']},
            headers=CSRF,
        )
        assert response.json()['note_id'] == 'new-note'
        assert remembered[0]['categories'] == ['property']

    def test_static_shell_is_public(self, client):
        assert client.get('/sw.js').status_code in (200, 404)  # 404 until static files exist
        assert client.get('/healthz').json()['status'] == 'ok'
