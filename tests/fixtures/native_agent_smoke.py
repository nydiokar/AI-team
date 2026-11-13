"""Credential-free Codex control smoke for the isolated worker image.

Creates an empty native thread and closes the app server. Never starts a turn.
Run only in a throwaway container with --network none and no host home mounts.
"""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from src.backends.codex_app_server import CodexAppServerClient


async def claude_control(root: str) -> None:
    executable = shutil.which("claude")
    assert executable is not None
    # This older external CLI waits for auth setup without a key. A synthetic,
    # non-authorizing value allows control initialization in --network none;
    # this smoke never submits a prompt or starts an inference request.
    client = ClaudeSDKClient(options=ClaudeAgentOptions(
        cli_path=executable, cwd=root, setting_sources=[],
        env={"HOME": root, "CLAUDE_CONFIG_DIR": str(Path(root) / "claude"),
             "ANTHROPIC_API_KEY": "offline-control-probe-no-inference",
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
                if child is not None:
                    assert child.returncode is not None, "external Claude child survived disconnect"
            finally:
                if child is not None:
                    if child.returncode is None:
                        child.kill()
                    await child.wait()
    assert child is not None


def main() -> None:
    executable = shutil.which("codex")
    assert executable is not None, "worker image must include its pinned Codex CLI"
    with tempfile.TemporaryDirectory(prefix="codex-native-offline-") as root:
        home = Path(root)
        (home / "codex").mkdir()
        client = CodexAppServerClient(executable, {
            "HOME": root, "CODEX_HOME": str(home / "codex"),
            "PATH": os.defpath + ":/usr/local/bin", "OTEL_SDK_DISABLED": "true",
        })
        process = None
        try:
            client.start()
            process = client.process
            assert process is not None and process.poll() is None
            response = client.attach_thread("", root, None, {})
            thread = response.get("thread")
            assert isinstance(thread, dict) and isinstance(thread.get("id"), str), response
            assert thread["id"]
        except Exception as exc:
            raise AssertionError(f"native app-server failed: {bytes(client.stderr).decode(errors='replace')}") from exc
        finally:
            client.close()
        assert process is not None and process.poll() is not None
        assert all(not reader.is_alive() for reader in client.readers)
        asyncio.run(claude_control(root))
        print(json.dumps({"codex_initialize": "passed", "empty_thread": "passed", "cleanup": "passed",
                          "external_claude_control": "passed", "model_turns": 0}))


if __name__ == "__main__":
    main()
