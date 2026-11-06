"""Exercise SDK teardown without connecting to an agent or using credentials."""

import subprocess
import sys

import anyio
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport


async def test_sdk_transport_reaps_child_in_cancelled_scope() -> None:
    """Use SDK teardown with a real, harmless child, without SDK connect/auth."""
    transport: SubprocessCLITransport = SubprocessCLITransport(
        prompt="offline", options=ClaudeAgentOptions(cli_path=sys.executable),
    )
    child = await anyio.open_process(
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
