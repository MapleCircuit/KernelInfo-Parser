"""webapp/main.py - KernelInfo-Parser Developer Web Application Entrypoint.

Starts the FastAPI server:
    python3 webapp/main.py [-c config.json] [--host HOST] [--port PORT] [--reload | --no-reload]
    uvicorn webapp.main:app --host 0.0.0.0 --port 8000 --reload
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
    """Command line of the server; unset options fall back to the `webapp` section of the config."""
    parser = argparse.ArgumentParser(description="KernelInfo-Parser Web Application Server")
    parser.add_argument("-c", "--config", type=str, default=None, help="Path to config.json file")
    parser.add_argument("--host", default=None, help="Host address to bind")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--reload", dest="reload", action="store_true", default=None, help="Enable auto-reload")
    parser.add_argument("--no-reload", dest="reload", action="store_false", help="Disable auto-reload")
    return parser.parse_args(argv)


def apply_config_path(args: argparse.Namespace) -> None:
    """Make `-c` the configuration of this process and of the one uvicorn's reloader starts: core.config
    reads CONFIG_FILE before falling back to the repository's config.json."""
    if args.config:
        os.environ["CONFIG_FILE"] = str(Path(args.config).resolve())


if __name__ == "__main__":
    cli = parse_args()
    apply_config_path(cli)
    import uvicorn
    from webapp.backend.config import get_backend_server_config

    cfg = get_backend_server_config()
    uvicorn.run(
        "webapp.main:app",
        host=cli.host or cfg.get("host", "0.0.0.0"),
        port=cli.port or int(cfg.get("port", 8000)),
        reload=bool(cfg.get("reload", True)) if cli.reload is None else cli.reload,
    )
else:
    from webapp.backend.app import create_app

    app = create_app()
