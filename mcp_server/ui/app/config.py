"""Settings for the memory manager UI, read from environment variables."""

from __future__ import annotations

import os
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
    max_login_failures: int = 5
    lockout_seconds: int = 900

    @property
    def password_configured(self) -> bool:
        return len(self.ui_password) >= 8

    @property
    def mcp_endpoint(self) -> str:
        return f'{self.mcp_url.rstrip("/")}/{self.mcp_secret_path}/mcp'


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
    )
