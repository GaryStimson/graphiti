"""Access control for exposing the MCP endpoint (e.g. through Tailscale Funnel).

The endpoint is mounted under an unguessable path segment so that only clients
given the full URL can reach it; every other path returns 404. This is a
capability URL: treat it like a password, and only serve it over HTTPS.
"""

import re

from mcp.server.transport_security import TransportSecuritySettings

MIN_SECRET_LENGTH = 32
_SECRET_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')


def validate_secret_path(secret_path: str) -> None:
    """Reject secrets that are too short or would not form a single URL path segment."""
    if len(secret_path) < MIN_SECRET_LENGTH:
        raise ValueError(
            f'secret_path must be at least {MIN_SECRET_LENGTH} characters; generate one with: '
            "python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    if not _SECRET_PATTERN.match(secret_path):
        raise ValueError('secret_path may only contain letters, digits, "-" and "_"')


def mcp_endpoint_path(secret_path: str | None) -> str:
    """Path the MCP endpoint is mounted at: /<secret>/mcp, or /mcp without a secret."""
    if not secret_path:
        return '/mcp'
    validate_secret_path(secret_path)
    return f'/{secret_path}/mcp'


def mask_secret_path(path: str, secret_path: str | None) -> str:
    """Hide the secret in a path before it is logged."""
    if not secret_path:
        return path
    return path.replace(secret_path, f'{secret_path[:4]}…')


def build_transport_security(allowed_hosts: list[str]) -> TransportSecuritySettings | None:
    """Enable DNS rebinding protection for the configured public hosts plus localhost.

    Returns None when no hosts are configured, leaving the MCP SDK default in place.
    """
    if not allowed_hosts:
        return None
    hosts = [*allowed_hosts, '127.0.0.1:*', 'localhost:*']
    origins = [f'https://{host}' for host in allowed_hosts] + [
        'http://127.0.0.1:*',
        'http://localhost:*',
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )
