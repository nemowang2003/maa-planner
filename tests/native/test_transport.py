"""Real loopback binding, token authentication and private endpoint publication."""

import asyncio
import json
import os

import pytest
import socketio
import socketio.exceptions

from maa_planner.client import ControlClient
from maa_planner.daemon.transport import ControlServer
from maa_planner.endpoint import read_endpoint


def test_endpoint_authentication_exclusive_binding_and_cleanup(tmp_path):
    async def handle(sid, message):
        return {"result": message}

    async def scenario():
        path = tmp_path / "private/control.json"
        server = ControlServer(path, handle)
        await server.start()
        try:
            endpoint = read_endpoint(path)
            assert endpoint.url.startswith("http://127.0.0.1:")
            assert endpoint.token not in repr(endpoint)
            if os.name != "nt":
                assert path.stat().st_mode & 0o777 == 0o600
            async with ControlClient(path) as client:
                assert await client.request({"method": "中文"}) == {"result": {"method": "中文"}}
                assert (await client.request([]))["error"]["code"] == "invalid_argument"
            for auth in (
                None,
                {"token": "wrong"},
                {"token": "中文"},
                {"token": endpoint.token, "extra": True},
            ):
                client = socketio.AsyncClient(reconnection=False)
                try:
                    with pytest.raises(socketio.exceptions.ConnectionError):
                        await client.connect(endpoint.url, auth=auth, transports=["websocket"])
                finally:
                    await client.disconnect()
                    await client.shutdown()
            with pytest.raises(RuntimeError):
                await ControlServer(path, handle).start()
        finally:
            await server.close()
        assert not path.exists()
        # Released locks and a fresh token permit a new daemon on the same endpoint.
        second = ControlServer(path, handle)
        await second.start()
        try:
            assert read_endpoint(path).token != endpoint.token
        finally:
            await second.close()

    asyncio.run(scenario())


def test_endpoint_preserves_unrelated_files(tmp_path):
    async def handle(sid, message):
        return {"result": None}

    async def scenario():
        path = tmp_path / "control.json"
        path.write_text("keep this file")
        with pytest.raises((ValueError, json.JSONDecodeError)):
            await ControlServer(path, handle).start()
        assert path.read_text() == "keep this file"

    asyncio.run(scenario())
