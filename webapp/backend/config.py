"""webapp/backend/config.py - Configuration management wrapper.

Resolves all database connection settings, webapp host/port bindings, and server options
through core.config with strict precedence:
    CLI Flags > Environment Variables > config.json > Built-in Defaults
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from core.config import (
    REPO_ROOT,
    get_db_config,
    get_webapp_config,
    get_parser_config,
    get_ssh_tunnel_config,
    init_config,
)

# Linux repository root path
LINUX_REPO_DIR = (REPO_ROOT / "linux").resolve()


def get_backend_db_config() -> dict[str, Any]:
    """Retrieve verified database configuration."""
    return get_db_config()


def get_backend_server_config() -> dict[str, Any]:
    """Retrieve verified web application server bindings."""
    return get_webapp_config()


def get_backend_ssh_tunnel_config() -> dict[str, Any]:
    """Retrieve verified SSH tunnel configuration."""
    return get_ssh_tunnel_config()
