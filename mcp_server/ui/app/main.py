"""Memory Manager UI: a small, password-protected web app beside the memory server."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import domain, ops
from .auth import COOKIE_NAME, Authenticator, csrf_ok
from .config import Settings, load_settings
from .graph import GraphStore
from .mcp_client import RememberError
from .mcp_client import remember as remember_via_mcp
from .parsing import parse_dt
from .sensitive import scan

logger = logging.getLogger('memory_ui')
STATIC_DIR = Path(__file__).resolve().parent.parent / 'static'

RememberFn = Callable[..., Awaitable[dict[str, Any]]]

SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Cross-Origin-Opener-Policy': 'same-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    'Content-Security-Policy': (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


class LoginBody(BaseModel):
    password: str = Field(max_length=256)


class FactEdit(BaseModel):
    action: str  # 'end' | 'restore'
    at: str | None = None


class RewriteBody(BaseModel):
    new_text: str = Field(min_length=3, max_length=2000)
    categories: list[str] | None = None
    delete_source_notes: bool = False
    allow_sensitive: bool = False


class RememberBody(BaseModel):
    content: str = Field(min_length=3, max_length=20000)
    categories: list[str] | None = None
    title: str | None = Field(default=None, max_length=120)
    allow_sensitive: bool = False


def create_app(
    settings: Settings | None = None,
    store: GraphStore | None = None,
    remember_fn: RememberFn | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    auth = Authenticator(settings)
    audit = ops.Audit(settings.data_dir)
    graph = store or GraphStore(
        settings.falkordb_host,
        settings.falkordb_port,
        settings.falkordb_password,
        settings.graph_name,
    )
    remember_call: RememberFn = remember_fn or (
        lambda **kw: remember_via_mcp(settings, **kw)
    )

    app = FastAPI(title='Memory Manager', docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()
    base = settings.base_path  # '' or '/<UI_SECRET_PATH>'

    def signed_in(request: Request) -> bool:
        """True if ANY mm_session cookie sent is valid (stale duplicates from an earlier path
        or deployment must not shadow the good one)."""
        for part in request.headers.get('cookie', '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == COOKIE_NAME and auth.session_valid(value):
                return True
        return False

    # ---------------------------------------------------------------- middleware

    @app.middleware('http')
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = (request.headers.get('host') or '').split(':')[0].lower()
        if host not in settings.allowed_hosts and request.url.path != '/healthz':
            return JSONResponse({'detail': 'Host not allowed'}, status_code=421)

        full_path = request.url.path
        if full_path == '/healthz':
            path = full_path
        elif base and (full_path == base or full_path.startswith(base + '/')):
            path = full_path[len(base):] or '/'
        elif base:
            # Not under the secret path: pretend nothing is here.
            return JSONResponse({'detail': 'Not found'}, status_code=404)
        else:
            path = full_path

        protected = path.startswith('/api/') or path in ('/auth/logout',)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and (
            path.startswith('/api/') or path.startswith('/auth/')
        ):
            if not csrf_ok(request.method, dict(request.headers), request.headers.get('host', '')):
                return JSONResponse({'detail': 'Cross-site request blocked'}, status_code=403)
        if protected and not signed_in(request):
            return JSONResponse({'detail': 'Not signed in'}, status_code=401)

        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if path.startswith(('/api/', '/auth/')):
            response.headers['Cache-Control'] = 'no-store'
        return response

    # ---------------------------------------------------------------- auth

    def client_id(request: Request) -> str:
        """Who is logging in: the proxy-supplied visitor IP when configured, else the peer."""
        if settings.trusted_ip_header:
            value = request.headers.get(settings.trusted_ip_header, '').split(',')[0].strip()
            if value:
                return value[:64]
        return request.client.host if request.client else 'unknown'

    @router.get('/auth/me')
    def me(request: Request) -> dict[str, Any]:
        return {
            'authenticated': signed_in(request),
            'configured': settings.password_configured,
        }

    @router.post('/auth/login')
    def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if not settings.password_configured:
            raise HTTPException(
                503, f'UI_PASSWORD is not set (needs at least {settings.min_password_length} characters)'
            )
        client = client_id(request)
        wait = auth.throttle.retry_after(client)
        if wait:
            raise HTTPException(429, f'Too many attempts. Try again in {wait} seconds.')
        if not auth.password_matches(body.password):
            auth.throttle.record_failure(client)
            logger.warning('Failed login from %s', client)
            raise HTTPException(401, 'Wrong password')
        auth.throttle.record_success(client)
        secure = (
            request.url.scheme == 'https'
            or request.headers.get('x-forwarded-proto', '').lower() == 'https'
        )
        response.set_cookie(
            COOKIE_NAME,
            auth.issue_session(),
            max_age=auth.max_age,
            httponly=True,
            samesite='lax',  # strict would be withheld when an installed app is launched from outside
            secure=secure,
            path=base or '/',
        )
        if base:
            response.delete_cookie(COOKIE_NAME, path='/')  # clear one from before the secret path
        return {'ok': True}

    @router.post('/auth/logout')
    def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE_NAME, path=base or '/')
        return {'ok': True}

    @app.get('/healthz')
    def healthz() -> dict[str, Any]:
        return {'status': 'ok', 'graph': graph.ping()}

    # ---------------------------------------------------------------- read API

    @router.get('/api/stats')
    def api_stats() -> dict[str, Any]:
        snap = graph.snapshot()
        data = domain.stats(snap)
        data['filters'] = {
            'categories': domain.categories_in_use(snap),
            'agents': domain.agents_in_use(snap),
        }
        return data

    @router.get('/api/notes')
    def api_notes(
        query: str = '', category: str = '', agent: str = '', days: int = 0, page: int = 1
    ) -> dict[str, Any]:
        return domain.list_notes(graph.snapshot(), query, category, agent, days, page)

    @router.get('/api/notes/{note_id}')
    def api_note(note_id: str) -> dict[str, Any]:
        data = domain.note_detail(graph.snapshot(), note_id)
        if data is None:
            raise HTTPException(404, 'Note not found')
        return data

    @router.get('/api/notes/{note_id}/delete-plan')
    def api_note_plan(note_id: str) -> dict[str, Any]:
        snap = graph.snapshot(fresh=True)
        note = snap.notes.get(note_id)
        if note is None:
            raise HTTPException(404, 'Note not found')
        return ops.public_plan(ops.plan_note_delete(snap, note))

    @router.get('/api/facts')
    def api_facts(
        status: str = '',
        query: str = '',
        category: str = '',
        agent: str = '',
        entity: str = '',
        page: int = 1,
    ) -> dict[str, Any]:
        return domain.list_facts(graph.snapshot(), status, query, category, agent, entity, page)

    @router.get('/api/facts/{fact_uuid}')
    def api_fact(fact_uuid: str) -> dict[str, Any]:
        snap = graph.snapshot()
        fact = snap.facts_by_uuid.get(fact_uuid)
        if fact is None:
            raise HTTPException(404, 'Fact not found')
        return domain.fact_dict(snap, fact)

    @router.get('/api/facts/{fact_uuid}/delete-plan')
    def api_fact_plan(fact_uuid: str) -> dict[str, Any]:
        snap = graph.snapshot(fresh=True)
        fact = snap.facts_by_uuid.get(fact_uuid)
        if fact is None:
            raise HTTPException(404, 'Fact not found')
        return ops.public_plan(ops.plan_fact_delete(snap, fact))

    @router.get('/api/entities')
    def api_entities(query: str = '', page: int = 1) -> dict[str, Any]:
        return domain.list_entities(graph.snapshot(), query, page)

    @router.get('/api/entities/{entity_uuid}')
    def api_entity(entity_uuid: str) -> dict[str, Any]:
        data = domain.entity_detail(graph.snapshot(), entity_uuid)
        if data is None:
            raise HTTPException(404, 'Entity not found')
        return data

    @router.get('/api/graph')
    def api_graph(
        center: str = '',
        hops: int = 1,
        status: str = 'current,not_yet_valid',
        category: str = '',
        query: str = '',
        limit: int = 80,
    ) -> dict[str, Any]:
        statuses = {s for s in status.split(',') if s} or None
        return domain.graph_view(
            graph.snapshot(),
            center=center,
            hops=hops,
            statuses=statuses,
            category=category,
            query=query,
            limit=max(10, min(limit, 300)),
        )

    @router.get('/api/review')
    def api_review(days: int = 7) -> dict[str, Any]:
        return domain.review(graph.snapshot(), max(1, min(days, 365)))

    @router.get('/api/changes')
    def api_changes() -> dict[str, Any]:
        return {'items': audit.entries()}

    @router.get('/api/export')
    def api_export() -> Response:
        data = domain.export(graph.snapshot(fresh=True))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        return JSONResponse(
            data, headers={'Content-Disposition': f'attachment; filename="memory-export-{stamp}.json"'}
        )

    # ---------------------------------------------------------------- write API

    @router.delete('/api/notes/{note_id}')
    def api_delete_note(note_id: str) -> dict[str, Any]:
        snap = graph.snapshot(fresh=True)
        note = snap.notes.get(note_id)
        if note is None:
            raise HTTPException(404, 'Note not found')
        return ops.delete_note(graph, audit, snap, note)

    @router.delete('/api/facts/{fact_uuid}')
    def api_delete_fact(fact_uuid: str) -> dict[str, Any]:
        snap = graph.snapshot(fresh=True)
        fact = snap.facts_by_uuid.get(fact_uuid)
        if fact is None:
            raise HTTPException(404, 'Fact not found')
        return ops.delete_fact(graph, audit, snap, fact)

    @router.patch('/api/facts/{fact_uuid}')
    def api_edit_fact(fact_uuid: str, body: FactEdit) -> dict[str, Any]:
        snap = graph.snapshot(fresh=True)
        fact = snap.facts_by_uuid.get(fact_uuid)
        if fact is None:
            raise HTTPException(404, 'Fact not found')
        if body.action == 'end':
            when = parse_dt(body.at) if body.at else None
            if body.at and when is None:
                raise HTTPException(422, 'Invalid date')
            return ops.end_fact(graph, audit, fact, when)
        if body.action == 'restore':
            return ops.restore_fact(graph, audit, fact)
        raise HTTPException(422, "action must be 'end' or 'restore'")

    def _check_sensitive(text: str, allow: bool) -> None:
        flags = scan(text)
        if flags and not allow:
            kinds = ', '.join(sorted({f.kind for f in flags}))
            raise HTTPException(
                422,
                f'This text looks like it contains sensitive data ({kinds}). '
                'Memory should not hold passwords, card numbers or one-time codes.',
            )

    @router.post('/api/remember')
    async def api_remember(body: RememberBody) -> dict[str, Any]:
        _check_sensitive(body.content, body.allow_sensitive)
        try:
            result = await remember_call(
                content=body.content, categories=body.categories, title=body.title
            )
        except RememberError as exc:
            raise HTTPException(502, str(exc)) from exc
        audit.record('remember', result.get('note_id', ''), f'Added memory: {body.content[:120]}')
        graph.invalidate()
        return result

    @router.post('/api/facts/{fact_uuid}/rewrite')
    async def api_rewrite_fact(fact_uuid: str, body: RewriteBody) -> dict[str, Any]:
        """Edit a fact's wording: delete it, then re-add the corrected sentence."""
        _check_sensitive(body.new_text, body.allow_sensitive)
        snap = graph.snapshot(fresh=True)
        fact = snap.facts_by_uuid.get(fact_uuid)
        if fact is None:
            raise HTTPException(404, 'Fact not found')
        shown = domain.fact_dict(snap, fact)
        categories = body.categories if body.categories is not None else shown['categories']
        source_notes = snap.fact_notes(fact)

        await run_in_threadpool(ops.delete_fact, graph, audit, snap, fact)
        removed_notes = []
        if body.delete_source_notes:
            for note in source_notes:
                fresh = graph.snapshot(fresh=True)
                current = fresh.notes.get(note.note_id)
                if current is not None:
                    await run_in_threadpool(ops.delete_note, graph, audit, fresh, current)
                    removed_notes.append(note.note_id)
        try:
            result = await remember_call(content=body.new_text, categories=categories or None)
        except RememberError as exc:
            raise HTTPException(
                502,
                f'The old fact was deleted but the new one could not be saved ({exc}). '
                'Add it again from "Add memory".',
            ) from exc
        audit.record(
            'rewrite_fact',
            fact_uuid,
            f'Rewrote fact to: {body.new_text[:120]}',
            new_note=result.get('note_id'),
        )
        graph.invalidate()
        return {'ok': True, 'new_note': result.get('note_id'), 'removed_notes': removed_notes}

    # ---------------------------------------------------------------- static / PWA

    def static_file(name: str, media_type: str | None = None, **headers: str) -> FileResponse:
        path = STATIC_DIR / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type=media_type, headers=headers or None)

    @router.get('/')
    def index() -> FileResponse:
        return static_file('index.html', 'text/html', **{'Cache-Control': 'no-cache'})

    @router.get('/sw.js')
    def service_worker() -> FileResponse:
        return static_file(
            'sw.js',
            'text/javascript',
            **{'Cache-Control': 'no-cache', 'Service-Worker-Allowed': f'{base}/'},
        )

    @router.get('/manifest.webmanifest')
    def manifest() -> FileResponse:
        return static_file('manifest.webmanifest', 'application/manifest+json')

    app.include_router(router, prefix=base)
    if STATIC_DIR.is_dir():
        app.mount(f'{base}/assets', StaticFiles(directory=STATIC_DIR / 'assets'), name='assets')
        app.mount(f'{base}/icons', StaticFiles(directory=STATIC_DIR / 'icons'), name='icons')

    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app()
