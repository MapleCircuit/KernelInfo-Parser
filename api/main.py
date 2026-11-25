"""api/main.py - KernelInfo-Parser Standalone AI API Entrypoint.

Starts the FastAPI server:
    python3 api/main.py [-c config.json] [--host HOST] [--port PORT] [--reload | --no-reload]
    uvicorn api.main:app --host 127.0.0.1 --port 8001
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command line of the AI API server; unset options fall back to the `api` section of config."""
    parser = argparse.ArgumentParser(description="KernelInfo-Parser Standalone AI API Server")
    parser.add_argument("-c", "--config", type=str, default=None, help="Path to config.json file")
    parser.add_argument("--host", default=None, help="Host address to bind")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--reload", dest="reload", action="store_true", default=None, help="Enable auto-reload")
    parser.add_argument("--no-reload", dest="reload", action="store_false", help="Disable auto-reload")
    return parser.parse_args(argv)


def apply_config_path(args: argparse.Namespace) -> None:
    """Propagate `-c` configuration path into environment variables and active core.config."""
    if args.config:
        from core.config import init_config

        resolved = Path(args.config).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Specified configuration file not found: {resolved}")
        resolved_str = str(resolved)
        os.environ["CONFIG_FILE"] = resolved_str
        os.environ["KERNELINFO_CONFIG"] = resolved_str
        init_config(resolved)


if __name__ == "__main__":
    cli = parse_args()
    apply_config_path(cli)
    import uvicorn

    from api.config import get_api_server_config
    from core.logging_setup import setup_webapp_logging

    setup_webapp_logging()
    cfg = get_api_server_config()
    uvicorn.run(
        "api.main:app",
        host=cli.host or cfg.get("host", "127.0.0.1"),
        port=cli.port or int(cfg.get("port", 8001)),
        reload=bool(cfg.get("reload", False)) if cli.reload is None else cli.reload,
    )
else:
    from api.app import create_app

    app = create_app()
