"""Real loopback WebSocket framing and cleanup; no gateway or agent runtime."""

import asyncio

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError


async def _echo(connection: ServerConnection) -> None:
    try:
        async for message in connection:
            await connection.send(message)
    except ConnectionClosedError:
        # Expected when the fragmentation limit rejects an abusive peer.
        pass


@pytest.mark.parametrize("fragments", [3, 4097])
async def test_websocket_fragment_limit(fragments: int) -> None:
    async with asyncio.timeout(10):
        async with serve(_echo, "127.0.0.1", 0) as server:
            port: int = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as client:
                await client.send(["x"] * fragments)
                if fragments == 3:
                    assert await client.recv() == "xxx"
                else:
                    with pytest.raises(ConnectionClosedError) as rejected:
                        await client.recv()
                    assert rejected.value.rcvd is not None
                    assert rejected.value.rcvd.code == 1009
