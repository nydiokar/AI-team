"""Offline transport tests. The fake executable cannot access a model provider."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.backends.codex_app_server import CodexAppServerClient, CodexProtocolError, CodexRPCError


@pytest.fixture
def server(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text('''#!/usr/bin/env python3
import json,sys,time,os
if "--version" in sys.argv:
    print(os.environ.get("FAKE_VERSION", "codex-cli 0.153.2")); sys.exit()
def emit(value):
    print(json.dumps(value),flush=True)
for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request: continue
    method=request["method"]
    if method=="initialize":
        emit({"id":request["id"],"result":{"codexHome":"/tmp","platformFamily":"unix","platformOs":"linux","userAgent":"codex/0.153.2"}})
        continue
    mode=request["params"].get("threadId", "")
    if mode=="die": sys.exit(1)
    if mode=="hang": time.sleep(10); continue
    if mode=="malformed": print("not json",flush=True); continue
    if mode=="wrong-id": emit({"id":99999,"result":{}}); continue
    if mode=="rejected":
        emit({"id":request["id"],"error":{"code":-32000,"message":"structured refusal","data":{"reason":"test"}}}); continue
    emit({"id":request["id"],"result":{}})
''')
    executable.chmod(0o700)
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
    client.close()
    assert process.poll() is not None
    assert not any(t.is_alive() for t in client.readers)


def test_version_rejected_before_runtime_spawn(server):
    client = server(FAKE_VERSION="codex-cli 0.1.0")
    with pytest.raises(CodexProtocolError, match="version_mismatch"):
        client.start()
    assert client.process is None


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


def test_deadline_poisons_connection_without_replay(server):
    client = server()
    client.start()
    with pytest.raises(CodexProtocolError, match="deadline"):
        client.request("turn/interrupt", {"threadId": "hang", "turnId": "turn"}, timeout=0.05)
    with pytest.raises(CodexProtocolError):
        client.request("turn/interrupt", {"threadId": "later", "turnId": "turn"})


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
