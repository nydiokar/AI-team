"""Exercise SDK teardown without connecting to an agent or using credentials."""

import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from anyio.abc import Process
from anyio.streams.text import TextReceiveStream
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport


async def test_sdk_transport_reaps_child_in_cancelled_scope() -> None:
    """Use SDK teardown with a real, harmless child, without SDK connect/auth."""
    transport: SubprocessCLITransport = SubprocessCLITransport(
        prompt="offline", options=ClaudeAgentOptions(cli_path=sys.executable),
    )
    child: Process = await anyio.open_process(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    transport._process = child
    try:
        with anyio.CancelScope() as scope:
            scope.cancel()
            await transport.close()
        assert child.returncode is not None, "SDK cancellation left its child alive"
        assert transport._process is None
    finally:
        # Reap even when asserting the old SDK's cleanup bug.
        with anyio.CancelScope(shield=True):
            if child.returncode is None:
                child.kill()
            await child.wait()
            await child.aclose()


def test_sdk_bundled_cli_version_without_model_call(tmp_path: Path) -> None:
    import claude_agent_sdk
    from claude_agent_sdk._cli_version import __cli_version__

    assert claude_agent_sdk.__file__ is not None
    executable = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    result = subprocess.run(
        [str(executable), "--version"], cwd=tmp_path,
        env={"HOME": str(tmp_path)}, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert __cli_version__ in result.stdout


async def test_sdk_real_bundled_cli_control_handshake_without_prompt(tmp_path: Path) -> None:
    """Initialize/interrupt/close the actual bundled CLI; never submit a prompt."""
    client = ClaudeSDKClient(options=ClaudeAgentOptions(
        cwd=str(tmp_path), setting_sources=[],
        env={"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"},
    ))
    child = None
    try:
        with anyio.fail_after(30):
            await client.connect()
            assert isinstance(client._transport, SubprocessCLITransport)
            child = client._transport._process
            assert child is not None and child.returncode is None
            await client.interrupt()
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await client.disconnect()
                assert child is not None and child.returncode is not None
            finally:
                if child is not None:
                    if child.returncode is None:
                        child.kill()
                    await child.wait()


async def test_sdk_transport_preserves_whitespace_at_chunk_seams() -> None:
    from src.backends.claude_driver import _resync_stdout_reader

    async def chunks():
        yield '{"type":"assistant","text":"keep '
        yield '  spaces"}\n'

    messages = [message async for message in _resync_stdout_reader(chunks(), 1024)]
    assert messages == [{"type": "assistant", "text": "keep   spaces"}]


async def test_sdk_transport_bounds_complete_frames_and_recovers() -> None:
    from src.backends.claude_driver import _resync_stdout_reader

    async def chunks():
        yield '{"text":"' + "x" * 1024 + '"}\n'
        yield '{"type":"result"}\n'

    messages = [message async for message in _resync_stdout_reader(chunks(), 128)]
    assert messages == [{"type": "result"}]


async def test_sdk_transport_resyncs_at_bound_without_losing_next_frame() -> None:
    from src.backends.claude_driver import _resync_stdout_reader

    async def chunks():
        yield '{"text":"' + "x" * 100
        yield '{"type":"result","ok":true}\n'

    messages = [message async for message in _resync_stdout_reader(chunks(), 128)]
    assert messages == [{"type": "result", "ok": True}]


async def test_sdk_patched_transport_checks_init_and_reaps_real_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backends import claude_driver

    monkeypatch.setattr(claude_driver, "_stream_resync_installed", False)
    # Restore the SDK class after this test, including when another test patched it.
    monkeypatch.setattr(SubprocessCLITransport, "_read_messages_impl", SubprocessCLITransport._read_messages_impl)
    claude_driver._install_sdk_stream_resync()
    transport = SubprocessCLITransport(prompt="offline", options=ClaudeAgentOptions(cli_path=sys.executable))
    child = await anyio.open_process(
        [sys.executable, "-c", 'print(\'{"type":"system","subtype":"init","claude_code_version":"2.1.300"}\')'],
        stdout=subprocess.PIPE,
    )
    assert child.stdout is not None
    transport._process = child
    transport._stdout_stream = TextReceiveStream(child.stdout)
    try:
        with anyio.fail_after(5):
            messages = [message async for message in transport.read_messages()]
        assert messages[0]["type"] == "system"
        assert transport._version_checked
        assert child.returncode == 0
    finally:
        await transport.close()
        if child.returncode is None:
            child.kill()
        await child.wait()


async def test_sdk_real_subprocess_create_continue_resume_and_disconnect(tmp_path: Path) -> None:
    """Real SDK and pipes against an offline protocol peer, never a paid CLI."""
    source = Path(__file__).parent / "fixtures" / "sdk_protocol_peer.py"
    peer = tmp_path / "sdk-peer"
    peer.write_text(source.read_text())
    peer.chmod(0o700)
    for resume in (None, "offline-session"):
        client = ClaudeSDKClient(options=ClaudeAgentOptions(cli_path=str(peer), cwd=str(tmp_path), resume=resume))
        child = None
        try:
            with anyio.fail_after(10):
                await client.connect()
                assert isinstance(client._transport, SubprocessCLITransport)
                child = client._transport._process
                for prompt in ("first", "continuation"):
                    await client.query(prompt)
                    messages = [message async for message in client.receive_response()]
                    assert any(isinstance(message, AssistantMessage) for message in messages)
                    result = messages[-1]
                    assert isinstance(result, ResultMessage)
                    assert result.session_id == "offline-session"
                    assert result.result == "offline reply"
                    assert not result.is_error
                await client.interrupt()
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await client.disconnect()
                    assert child is not None and child.returncode is not None, "SDK disconnect left a live child"
                finally:
                    if child is not None:
                        if child.returncode is None:
                            child.kill()
                        await child.wait()
        assert child is not None and child.returncode is not None


def test_application_sdk_session_with_actual_sdk_and_protocol_peer(tmp_path: Path) -> None:
    import psutil

    from src.backends.claude_driver import _SDKSession
    from src.core.turn_liveness import turn_control

    peer = tmp_path / "sdk-peer"
    peer.write_text((Path(__file__).parent / "fixtures" / "sdk_protocol_peer.py").read_text())
    peer.chmod(0o700)
    for resume in (None, "offline-session"):
        session = _SDKSession("offline", str(tmp_path), None, {}, cli_path=str(peer), resume=resume)
        try:
            session.start()
            assert session.pid > 0
            for index in (1, 2):
                control = turn_control(f"00000000-0000-4000-8000-{index:012d}", stall_override=5)
                result = session.send("offline query", turn=control)
                assert result.output == "offline reply"
                assert result.backend_session_id == "offline-session"
                assert not result.is_error
                assert session.is_quiescent()
        finally:
            session.close()
            if session._thread is not None:
                session._thread.join(timeout=10)
                assert not session._thread.is_alive(), "application SDK loop did not stop"
            assert not session.pid or not psutil.pid_exists(session.pid), "application left its SDK child alive"
