#!/usr/bin/env python3
"""
PM2 entry point for the standalone mesh task server.

State Separation Phase 2: the task server can run as its own process instead of
embedded inside the gateway (src/control/embedded_server.py). This thin launcher
mirrors worker_main.py — it puts ./src on sys.path, loads .env, inits the shared
observability spine, then runs uvicorn on src.control.task_server:app.

Run directly (no PM2 required):
    python server_main.py

Binds {MESH_TAILSCALE_IP or 127.0.0.1}:{MESH_TASK_SERVER_PORT}. Reads the same
.env and the same state/mesh.db as the gateway and worker.
"""
import sys
from pathlib import Path

# Match main.py / worker_main.py: append (not prepend) src so we don't shadow
# third-party packages that share a top-level name with our modules.
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

    # node_id="controller" so every task-server log line is distinguishable from
    # gateway/worker lines in a shared events stream (correlate by task_id).
    init_logging(node_id="controller", level="INFO")

    host = config.mesh.tailscale_ip or "127.0.0.1"
    port = config.mesh.task_server_port

    import uvicorn

    uvicorn.run(
        "src.control.task_server:app",
        host=host,
        port=port,
        log_level="warning",
        lifespan="on",
    )


if __name__ == "__main__":
    main()
