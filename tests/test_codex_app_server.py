"""Offline transport tests. The fake executable cannot access a model provider."""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.backends.codex_app_server import (
    CodexAppServerClient, CodexProtocolError, CodexRPCError, CodexRPCTimeout,
)


@pytest.fixture
def server(tmp_path):
    script = tmp_path / "codex.py"
    script.write_text('''#!/usr/bin/env python3
import json,sys,time,os
if "--version" in sys.argv:
    sys.exit(99)
def emit(value):
    print(json.dumps(value),flush=True)
for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request: continue
    method=request["method"]
    if method=="initialize":
        emit({"id":request["id"],"result":{"codexHome":"/tmp","platformFamily":"unix","platformOs":"linux","userAgent":"codex/test"}})
        continue
    mode=request["params"].get("threadId", "")
    if mode=="die": sys.exit(1)
    if mode=="hang": time.sleep(10); continue
    if mode=="slow": time.sleep(0.5)
    if mode=="malformed": print("not json",flush=True); continue
    if mode=="wrong-id": emit({"id":99999,"result":{}}); continue
    if mode=="unknown":
        emit({"method":"thread/custom","params":{"threadId":"unknown"}})
    if mode=="rejected":
        emit({"id":request["id"],"error":{"code":-32000,"message":"structured refusal","data":{"reason":"test"}}}); continue
    emit({"id":request["id"],"result":{}})
''')
    executable = script
    if os.name == "nt":
        executable = tmp_path / "codex.cmd"
        executable.write_text(f'@"{sys.executable}" "%~dp0codex.py" %*\r\n')
    else:
        script.chmod(0o700)
    clients = []
    def make(**env):
        client = CodexAppServerClient(str(executable), {**os.environ, **env})
        clients.append(client)
        return client
    yield make
    for client in clients:
        client.close()
        assert all(not reader.is_alive() for reader in client.readers)


def test_initialize_and_clean_shutdown(server):
    client = server()
    client.start()
    process = client.process
    if os.name == "nt":
        assert client.windows_job is not None
    client.close()
    assert process.poll() is not None
    assert client.windows_job is None
    assert not any(t.is_alive() for t in client.readers)


def test_runtime_start_does_not_probe_version(server):
    client = server()
    client.start()
    assert client.process is not None


@pytest.mark.parametrize("mode", ["die", "malformed", "wrong-id"])
def test_protocol_failure_wakes_waiting_call(server, mode):
    client = server()
    client.start()
    with pytest.raises(CodexProtocolError):
        client.request("turn/interrupt", {"threadId": mode, "turnId": "turn"}, timeout=1)


def test_rpc_error_retains_structured_payload(server):
    client = server()
    client.start()
    with pytest.raises(CodexRPCError) as error:
        client.request("turn/interrupt", {"threadId": "rejected", "turnId": "turn"})
    assert error.value.error["data"] == {"reason": "test"}
    assert not client.failure


def test_unknown_notification_does_not_poison_runtime(server):
    client = server()
    client.start()
    client.subscribe("unknown")
    assert client.request("turn/interrupt", {"threadId": "unknown", "turnId": "turn"}) == {}
    assert not client.failure


def test_deadline_is_request_scoped_and_late_reply_is_routed_not_fatal(server):
    """[A82 step 4 rework, M1] Replaces "deadline poisons the connection": one
    slow reply must never fail the SHARED client (that killed every other
    thread's turn). The late reply goes to ``on_late`` or is dropped; never
    replayed, never an unmatched-response failure."""
    client = server()
    client.start()
    late: list[dict] = []
    with pytest.raises(CodexRPCTimeout, match="deadline"):
        client.request("turn/interrupt", {"threadId": "slow", "turnId": "turn"}, timeout=0.05,
                       on_late=late.append)
    assert not client.failure
    assert client.request("turn/interrupt", {"threadId": "later", "turnId": "turn"}) == {}
    assert len(late) == 1 and late[0]["result"] == {}
    with pytest.raises(CodexRPCTimeout):
        client.request("turn/interrupt", {"threadId": "slow", "turnId": "turn"}, timeout=0.05)
    assert client.request("turn/interrupt", {"threadId": "after", "turnId": "turn"}) == {}
    assert not client.failure and client.process.poll() is None
    assert client.pending == {} and client.late == {}


def test_concurrent_rpc_ids_are_correlated(server):
    client = server()
    client.start()
    barrier = threading.Barrier(8)
    def invoke(index):
        barrier.wait(timeout=2)
        return client.request("turn/interrupt", {"threadId": str(index), "turnId": "turn"})
    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(invoke, range(8))) == [{}] * 8
    assert client.pending == {}


def test_thread_channels_reject_competing_subscription(server):
    client = server()
    client.start()
    client.subscribe("exact")
    with pytest.raises(CodexProtocolError, match="thread_busy"):
        client.subscribe("exact")
    client.subscribe("different")
    client.unsubscribe("exact")
    client.subscribe("exact")


def test_late_ids_are_bounded_by_refusal_never_by_eviction(server, monkeypatch):
    """[A82 step 4 rework, M1] An evicted late id would make its reply read as
    unmatched and fail the shared client: the bound refuses new requests
    (request-scoped) until late replies drain."""
    import src.backends.codex_app_server as mod

    monkeypatch.setattr(mod, "MAX_LATE", 1)
    client = server()
    client.start()
    with pytest.raises(CodexRPCTimeout):
        client.request("turn/interrupt", {"threadId": "slow", "turnId": "turn"}, timeout=0.05)
    with pytest.raises(CodexProtocolError, match="capacity"):
        client.request("turn/interrupt", {"threadId": "x", "turnId": "turn"})
    assert not client.failure
    deadline = time.monotonic() + 5
    while client.late and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.request("turn/interrupt", {"threadId": "after", "turnId": "turn"}) == {}
    assert not client.failure


def test_hung_request_goes_late_then_unresponsive_and_its_route_can_be_forgotten(server):
    """[A82 pre-cutover, N1] Restores the ``hang``-mode coverage: a request the
    app-server never answers is request-scoped (the client stays healthy), is
    tracked as outstanding, flags the app-server unresponsive past a window,
    and its late route can be dropped without un-matching a future reply."""
    client = server()
    client.start()
    with pytest.raises(CodexRPCTimeout) as info:
        client.request("turn/interrupt", {"threadId": "hang", "turnId": "turn"}, timeout=0.05)
    request_id = info.value.request_id
    assert request_id > 0 and client.outstanding(request_id)
    assert client.unresponsive(5.0) is False
    time.sleep(0.2)
    assert client.unresponsive(0.1) is True
    assert client.forget_late(request_id) is True
    assert client.late == {} and client.outstanding(request_id)
    assert not client.failure and client.process.poll() is None
