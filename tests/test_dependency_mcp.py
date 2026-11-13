"""Real MCP client/stdio server compatibility, without a gateway or paid agent."""

import asyncio
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


@pytest.mark.parametrize("server", ["jobs", "manager", "sender"])
async def test_mcp_stdio_discovery_and_invocation(server: str, tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / f"mcp_{server}.py"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(script)],
        env={
            "HOME": str(tmp_path), "AI_TEAM_TEST_MODE": "1",
            "AI_TEAM_SESSION_ID": "", "SESSION_ID": "",
            "DASHBOARD_TOKEN": "", "WORKER_TOKEN": "",
            "PYTHONPATH": os.pathsep.join(sys.path),
        },
    )
    with anyio.fail_after(15):
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            initialized = await session.initialize()
            assert initialized.capabilities.tools is not None
            tools = await session.list_tools()
            assert tools.tools
            assert all(tool.model_dump(by_alias=True)["inputSchema"]["type"] == "object" for tool in tools.tools)
            if server != "sender":
                result = await session.call_tool("whoami", {})
                assert not result.model_dump(by_alias=True)["isError"]
                assert isinstance(result.content[0], TextContent)
                assert "identity is not present" in result.content[0].text
            else:
                # A real structured tool failure, rejected before any HTTP call.
                result = await session.call_tool(tools.tools[0].name, {})
                assert result.model_dump(by_alias=True)["isError"]
                assert result.content


async def test_sdk_mcp_bridge_invokes_actual_sender_adapter() -> None:
    """The SDK's MCP-2 bridge executes our actual in-process sender server."""
    from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

    from src.control.agent_sender import SenderSlot, build_claude_sender_server

    definition = build_claude_sender_server(SenderSlot())
    bridge = SdkMcpBridge("offline-sender", definition["instance"])
    try:
        with anyio.fail_after(10):
            initialized = await bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "offline", "version": "1"},
            }})
            assert initialized is not None and "result" in initialized
            await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert listed is not None
            assert listed["result"]["tools"][0]["name"] == "send_instruction"
            result = await bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "send_instruction", "arguments": {
                    "target_session_id": "offline", "body": "test", "operation_id": "offline-1",
                },
            }})
            assert result is not None
            assert result["result"]["isError"]
            assert "not provisioned" in result["result"]["content"][0]["text"]
    finally:
        await bridge.aclose()


async def test_sdk_mcp_bridge_cancels_inflight_tool_on_close() -> None:
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

    started = asyncio.Event()
    stopped = asyncio.Event()

    @tool("wait", "Offline cancellation probe", {})
    async def wait_tool(args: dict[str, object]) -> dict[str, object]:
        try:
            started.set()
            await asyncio.Event().wait()
            return {"content": []}
        finally:
            stopped.set()

    definition = create_sdk_mcp_server(name="offline", tools=[wait_tool])
    bridge = SdkMcpBridge("offline", definition["instance"])
    request = None
    try:
        with anyio.fail_after(10):
            await bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "offline", "version": "1"},
            }})
            await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            request = asyncio.create_task(bridge.handle({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "wait", "arguments": {}},
            }))
            await started.wait()
            await bridge.aclose()
            await stopped.wait()
            result = await request
            assert result is not None
            assert result["error"]["code"] == -32800
    finally:
        await bridge.aclose()
        if request is not None:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
