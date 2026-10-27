"""Settings for the memory manager UI, read from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _split(value: str | None, default: list[str]) -> list[str]:
    items = [item.strip().lower() for item in (value or '').split(',') if item.strip()]
    return items or default


@dataclass(frozen=True)
class Settings:
    ui_password: str
    data_dir: Path
    falkordb_host: str
    falkordb_port: int
    falkordb_password: str
    graph_name: str
    mcp_url: str
    mcp_secret_path: str
    mcp_host_header: str
    allowed_hosts: list[str] = field(default_factory=list)
    session_days: int = 14
    trusted_ip_header: str = ''
    ui_secret_path: str = ''
    min_password_length: int = 12
    max_login_failures: int = 5
    lockout_seconds: int = 900

    @property
    def password_configured(self) -> bool:
        return len(self.ui_password) >= self.min_password_length

    @property
    def base_path(self) -> str:
        """URL prefix the whole UI is served under ('' when no secret path is set)."""
        return f'/{self.ui_secret_path}' if self.ui_secret_path else ''

    @property
    def mcp_endpoint(self) -> str:
        return f'{self.mcp_url.rstrip("/")}/{self.mcp_secret_path}/mcp'


def _secret_path(value: str | None) -> str:
    secret = (value or '').strip().strip('/')
    if secret and (len(secret) < 32 or not re.fullmatch(r'[A-Za-z0-9_-]+', secret)):
        raise ValueError(
            'UI_SECRET_PATH must be at least 32 characters from A-Z, a-z, 0-9, _ and -'
        )
    return secret


def load_settings() -> Settings:
    return Settings(
        ui_password=os.getenv('UI_PASSWORD', ''),
        data_dir=Path(os.getenv('UI_DATA_DIR', '/data')),
        falkordb_host=os.getenv('FALKORDB_HOST', 'personal-memory'),
        falkordb_port=int(os.getenv('FALKORDB_PORT', '6379')),
        falkordb_password=os.getenv('FALKORDB_PASSWORD', ''),
        graph_name=os.getenv('GRAPHITI_GROUP_ID', 'personal'),
        mcp_url=os.getenv('MCP_URL', 'http://personal-memory:8000'),
        mcp_secret_path=os.getenv('MCP_SECRET_PATH', ''),
        # The memory server's DNS-rebinding protection only knows localhost and the public
        # hostnames, so internal calls present themselves as localhost.
        mcp_host_header=os.getenv('MCP_HOST_HEADER', 'localhost:8000'),
        allowed_hosts=_split(os.getenv('UI_ALLOWED_HOSTS'), ['localhost', '127.0.0.1']),
        session_days=int(os.getenv('UI_SESSION_DAYS', '14')),
        # Set to e.g. cf-connecting-ip when served through a Cloudflare Tunnel, so login
        # throttling counts the real visitor and not the tunnel for every request.
        trusted_ip_header=os.getenv('UI_TRUSTED_IP_HEADER', '').strip().lower(),
        ui_secret_path=_secret_path(os.getenv('UI_SECRET_PATH')),
    )
