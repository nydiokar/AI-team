"""SESSION_ID exported to Claude children must be the GATEWAY session id.

`mcp_jobs.watch_job` stamps os.environ["SESSION_ID"] onto the job row, and the
orchestrator resolves the job's owner with session_store.get(job.session_id).
Exporting the Claude backend UUID (or a random uuid4 on the first turn) made
every Claude-registered job unresolvable -> `job_notify_skipped reason=no_session`
and an "unlinked session" job in the UI.
"""
from src.backends.claude_code import ClaudeCodeBackend
from src.core import test_guard
from src.core.interfaces import ExecutionResult, Session, SessionStatus


class _CapturingDriver:
    def __init__(self) -> None:
        self.envs: list[dict] = []

    def driver_type(self) -> str:
        return "sdk"

    def _capture(self, proc_env: dict) -> ExecutionResult:
        self.envs.append(dict(proc_env))
        return ExecutionResult(success=True, output="ok", errors=[])

    def start_session(self, session, message, *, model=None, telemetry_context=None, proc_env=None) -> ExecutionResult:
        return self._capture(proc_env or {})

    def send_turn(self, session, message, *, model=None, telemetry_context=None, proc_env=None) -> ExecutionResult:
        return self._capture(proc_env or {})


def _backend(monkeypatch) -> tuple[ClaudeCodeBackend, _CapturingDriver]:
    monkeypatch.setattr(test_guard, "assert_live_calls_allowed", lambda *_a, **_k: None)
    backend = ClaudeCodeBackend()
    driver = _CapturingDriver()
    backend._driver = driver
    return backend, driver


def _session(backend_session_id: str | None) -> Session:
    session = Session(
        session_id="5725a4bf260c", backend="claude", repo_path="",
        status=SessionStatus.IDLE, created_at="2026-09-19T00:00:00Z", updated_at="2026-09-19T00:00:00Z",
    )
    session.backend_session_id = backend_session_id
    session.last_user_message = "hi"
    return session


def test_create_session_exports_gateway_session_id(monkeypatch) -> None:
    backend, driver = _backend(monkeypatch)
    backend.create_session(_session(None))
    assert driver.envs[0]["SESSION_ID"] == "5725a4bf260c"


def test_resume_session_exports_gateway_not_backend_session_id(monkeypatch) -> None:
    backend, driver = _backend(monkeypatch)
    backend.resume_session(_session("c392358c-a4a0-47ed-9b12-f11b6b53a852"), "next")
    assert driver.envs[0]["SESSION_ID"] == "5725a4bf260c"
