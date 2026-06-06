"""
Worker daemon — one per machine, managed by PM2.

Lifecycle:
  1. Read config from env (WORKER_NODE_ID, WORKER_TOKEN, etc.)
  2. POST /nodes/register
  3. Start nudge listener on WORKER_TAILSCALE_IP:WORKER_API_PORT
  4. Poll /tasks/pending with adaptive backoff (5s → 30s on empty, resets on task received)
  5. Claim → execute locally using existing src/backends/ → POST /tasks/{id}/result
  6. Heartbeat every 30s concurrently
  7. On SIGTERM: deregister, drain active tasks (up to 30s), exit

Run locally (no Tailscale required):
    WORKER_NODE_ID=main-pc WORKER_TOKEN=<token> WORKER_TAILSCALE_IP=127.0.0.1 \\
    CONTROLLER_URL=http://127.0.0.1:9002 WORKER_BACKENDS=claude,opencode \\
    python -m src.worker.agent
"""

import asyncio
import json
import logging
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helpers — stdlib only, no httpx/requests required
# ---------------------------------------------------------------------------

class _HTTP:
    """Minimal synchronous HTTP wrapper using urllib."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def post(self, path: str, body: Any = None, timeout: int = 10) -> Any:
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def get(self, path: str, params: Optional[Dict[str, str]] = None, timeout: int = 10) -> Any:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Nudge listener — tiny asyncio HTTP server, just accepts POST /nudge
# ---------------------------------------------------------------------------

async def _run_nudge_listener(host: str, port: int, poll_event: asyncio.Event) -> None:
    """Accept POST /nudge and set poll_event so the poll loop fires immediately."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(512), timeout=2)
            # Minimal method/path validation — avoid treating arbitrary TCP
            # probes (port scanners, health checks) as a real nudge, which
            # would reset the poll backoff and cause spurious tight polling.
            is_nudge = data.startswith(b"POST /nudge")
            if is_nudge:
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
                poll_event.set()
                logger.debug("event=nudge_received")
            else:
                response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(_handler, host, port)
        logger.info("event=nudge_listener_started host=%s port=%d", host, port)
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.warning("event=nudge_listener_failed err=%s", e)


# ---------------------------------------------------------------------------
# Backend instantiation
# ---------------------------------------------------------------------------

def _make_backends() -> Dict[str, Any]:
    from src.backends import ClaudeCodeBackend, CodexBackend, OpenCodeBackend, OpenCodeServerBackend
    return {
        "claude": ClaudeCodeBackend(),
        "codex": CodexBackend(),
        "opencode": OpenCodeBackend(),
        "opencode-server": OpenCodeServerBackend(),
    }


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def _make_session_from_payload(payload: Dict[str, Any]) -> Any:
    """Reconstruct a Session-like object from the task payload."""
    from src.core import SessionStore
    from src.core.interfaces import Session, SessionStatus

    session_dict = payload.get("session")
    if not session_dict:
        return None

    # Build minimal Session
    session = Session(
        session_id=session_dict.get("session_id", ""),
        backend=session_dict.get("backend", "claude"),
        repo_path=session_dict.get("repo_path", ""),
        status=SessionStatus.BUSY,
        created_at=session_dict.get("created_at", datetime.now(tz=timezone.utc).isoformat()),
        updated_at=datetime.now(tz=timezone.utc).isoformat(),
        machine_id=session_dict.get("machine_id", ""),
        backend_session_id=session_dict.get("backend_session_id", ""),
    )
    # Copy optional fields if present
    for attr in ("telegram_chat_id", "telegram_thread_id", "owner_user_id", "last_user_message"):
        if attr in session_dict:
            setattr(session, attr, session_dict[attr])
    return session


# ---------------------------------------------------------------------------
# Task executor
# ---------------------------------------------------------------------------

async def _execute_task(task_row: Dict[str, Any], backends: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one task row from mesh_tasks. Returns an ExecutionResultPayload-compatible dict."""
    payload = task_row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    action = task_row.get("action", "run_oneoff")
    backend_name = task_row.get("backend", "claude")
    backend = backends.get(backend_name)
    if backend is None:
        return {
            "success": False,
            "errors": [f"Backend {backend_name!r} not available on this worker"],
            "output": "",
            "files_modified": [],
            "execution_time": 0.0,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 1,
        }

    prompt = payload.get("prompt", "")
    start = time.monotonic()

    try:
        from src.core.interfaces import ExecutionResult as _ER

        if action in ("create_session", "resume_session"):
            session = _make_session_from_payload(payload)
            if session is None:
                raise ValueError("Session payload missing for session action")
            if action == "create_session" or not session.backend_session_id:
                raw = await asyncio.to_thread(backend.create_session, session)
            else:
                raw = await asyncio.to_thread(backend.resume_session, session, prompt)
        else:
            cwd = payload.get("metadata", {}).get("cwd", "")
            raw = await asyncio.to_thread(backend.run_oneoff, cwd, prompt)

        elapsed = time.monotonic() - start
        if isinstance(raw, _ER):
            return {
                "success": raw.success,
                "output": (raw.output or "")[:4000],
                "errors": list(raw.errors or []),
                "files_modified": list(raw.files_modified or []),
                "execution_time": elapsed,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "return_code": getattr(raw, "return_code", 0),
            }
        # Fallback for legacy return types
        return {
            "success": True,
            "output": str(raw)[:4000],
            "errors": [],
            "files_modified": [],
            "execution_time": elapsed,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 0,
        }
    except Exception as e:
        elapsed = time.monotonic() - start
        logger.error("event=execute_task_failed task_id=%s err=%s", task_row.get("id"), e)
        return {
            "success": False,
            "output": "",
            "errors": [str(e)],
            "files_modified": [],
            "execution_time": elapsed,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 1,
        }


# ---------------------------------------------------------------------------
# Worker daemon
# ---------------------------------------------------------------------------

class WorkerAgent:
    def __init__(self) -> None:
        from src.worker.config import WorkerConfig
        self.cfg = WorkerConfig.from_env()
        self._http = _HTTP(self.cfg.controller_url, self.cfg.worker_token)
        self._backends = _make_backends()
        self._active: Dict[str, asyncio.Task] = {}   # task_id → asyncio.Task
        self._shutdown = asyncio.Event()
        self._poll_now = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self.cfg.max_concurrent)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        self._http.post("/nodes/register", {
            "node_id": self.cfg.node_id,
            "tailscale_ip": self.cfg.tailscale_ip,
            "api_port": self.cfg.api_port,
            "capabilities": {
                "backends": self.cfg.backends,
                "max_concurrent": self.cfg.max_concurrent,
            },
        })
        logger.info("event=registered node_id=%s controller=%s", self.cfg.node_id, self.cfg.controller_url)

    def _deregister(self) -> None:
        try:
            self._http.post("/nodes/deregister", {"node_id": self.cfg.node_id})
            logger.info("event=deregistered node_id=%s", self.cfg.node_id)
        except Exception as e:
            logger.warning("event=deregister_failed err=%s", e)

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._shutdown.is_set():
                try:
                    await asyncio.to_thread(
                        self._http.post, "/nodes/heartbeat", {"node_id": self.cfg.node_id}
                    )
                    logger.debug("event=heartbeat_sent node_id=%s", self.cfg.node_id)
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        # Server doesn't know us — likely restarted and lost
                        # its in-memory registry. Re-register so the mesh
                        # doesn't go dark silently.
                        logger.warning(
                            "event=heartbeat_node_unknown node_id=%s — re-registering",
                            self.cfg.node_id,
                        )
                        try:
                            await asyncio.to_thread(self._register)
                        except Exception as re_err:
                            logger.warning("event=re_register_failed err=%s", re_err)
                    else:
                        logger.warning("event=heartbeat_failed status=%s err=%s", e.code, e)
                except Exception as e:
                    logger.warning("event=heartbeat_failed err=%s", e)
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        empty_count = 0
        try:
            while not self._shutdown.is_set():
                tasks = await self._fetch_pending()
                if tasks:
                    empty_count = 0
                    for row in tasks:
                        if self._shutdown.is_set():
                            break
                        task_id = row.get("id", "unknown")
                        t = asyncio.create_task(self._handle_task(row))
                        self._active[task_id] = t
                        t.add_done_callback(lambda _t, tid=task_id: self._active.pop(tid, None))
                    # Short pause to avoid hammering if tasks are always present
                    wait_sec = 2
                else:
                    empty_count += 1
                    # Back off to 30s after 5 empty polls
                    wait_sec = 30 if empty_count >= 5 else 5

                # Wait for next poll interval or a nudge
                self._poll_now.clear()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._poll_now.wait()),
                        timeout=wait_sec,
                    )
                    empty_count = 0  # nudge received — reset backoff
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _fetch_pending(self) -> List[Dict[str, Any]]:
        try:
            return await asyncio.to_thread(
                self._http.get,
                "/tasks/pending",
                {
                    "node_id": self.cfg.node_id,
                    "backends": ",".join(self.cfg.backends),
                    "limit": str(self.cfg.max_concurrent * 2),
                },
            )
        except Exception as e:
            logger.warning("event=fetch_pending_failed err=%s", e)
            return []

    # ------------------------------------------------------------------
    # Task handling
    # ------------------------------------------------------------------

    async def _handle_task(self, task_row: Dict[str, Any]) -> None:
        task_id = task_row.get("id", "unknown")
        async with self._semaphore:
            # Claim — optimistic lock
            try:
                resp = await asyncio.to_thread(
                    self._http.post,
                    f"/tasks/{task_id}/claim",
                    {"node_id": self.cfg.node_id},
                )
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    logger.debug("event=claim_race task_id=%s", task_id)
                else:
                    logger.warning("event=claim_failed task_id=%s err=%s", task_id, e)
                return
            except Exception as e:
                logger.warning("event=claim_failed task_id=%s err=%s", task_id, e)
                return

            logger.info("event=task_claimed task_id=%s node=%s", task_id, self.cfg.node_id)

            # Execute
            result = await _execute_task(task_row, self._backends)

            # Post result
            try:
                await asyncio.to_thread(
                    self._http.post,
                    f"/tasks/{task_id}/result",
                    {"node_id": self.cfg.node_id, **result},
                )
                logger.info(
                    "event=task_result_posted task_id=%s success=%s elapsed=%.1fs",
                    task_id, result["success"], result["execution_time"],
                )
            except Exception as e:
                logger.error("event=result_post_failed task_id=%s err=%s", task_id, e)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._register()

        nudge_listener = asyncio.create_task(
            _run_nudge_listener(self.cfg.tailscale_ip, self.cfg.api_port, self._poll_now)
        )
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        poller = asyncio.create_task(self._poll_loop())

        # Install SIGTERM handler
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)

        await self._shutdown.wait()

        # Drain: wait up to 30s for active tasks
        logger.info("event=draining active=%d", len(self._active))
        if self._active:
            done, pending = await asyncio.wait(
                list(self._active.values()), timeout=30
            )
            for t in pending:
                t.cancel()

        for t in (poller, heartbeat, nudge_listener):
            t.cancel()
        await asyncio.gather(poller, heartbeat, nudge_listener, return_exceptions=True)

        self._deregister()

    def _on_sigterm(self) -> None:
        logger.info("event=sigterm_received node_id=%s", self.cfg.node_id)
        self._shutdown.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    agent = WorkerAgent()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
