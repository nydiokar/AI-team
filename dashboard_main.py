#!/usr/bin/env python3
"""
PM2 / standalone entry point for the read-only Cockpit web dashboard (M3).

Mirrors server_main.py: puts ./src on sys.path, loads .env, inits the shared
observability spine, then runs uvicorn on src.control.dashboard:app. The
dashboard is read-only — it consumes the same state/mesh.db and logs/events.ndjson
as the gateway; it issues no commands.

Run directly:
    python dashboard_main.py

Binds 127.0.0.1:{DASHBOARD_PORT} (default 9003). Auth: DASHBOARD_TOKEN
(falls back to WORKER_TOKEN).
"""
import sys
from pathlib import Path

src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.append(str(src_path))


def main() -> None:
    try:
        from dotenv import load_dotenv
        _env = Path(__file__).resolve().parent / ".env"
        if _env.exists():
            load_dotenv(_env, override=False)
    except ImportError:
        pass

    from config import config
    from src.core.observability import init_logging

    init_logging(node_id="dashboard", level="INFO")

    host = "127.0.0.1"
    port = config.mesh.dashboard_port

    import uvicorn

    uvicorn.run(
        "src.control.dashboard:app",
        host=host,
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
