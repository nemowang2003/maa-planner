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


def test_endpoint_preserves_valid_looking_foreign_descriptor(tmp_path):
    async def handle(sid, message):
        return {"result": None}

    async def scenario():
        path = tmp_path / "private/control.json"
        path.parent.mkdir(mode=0o700)
        foreign = (
            b'{"url":"http://127.0.0.1:12345","token":"foreign-owner-foreign-owner-foreign-owner"}'
        )
        path.write_bytes(foreign)
        server = ControlServer(path, handle)
        try:
            with pytest.raises(ValueError, match="归属|属于"):
                await server.start()
            assert path.read_bytes() == foreign
        finally:
            await server.close()

    asyncio.run(scenario())


def test_endpoint_replaces_its_own_stale_descriptor_after_crash(tmp_path):
    async def handle(sid, message):
        return {"result": None}

    async def scenario():
        path = tmp_path / "private/control.json"
        first = ControlServer(path, handle)
        await first.start()
        stale = path.read_bytes()
        await first.close()
        # Simulate a crash that left only our descriptor behind, with the lock released.
        path.write_bytes(stale)
        second = ControlServer(path, handle)
        await second.start()
        try:
            assert read_endpoint(path).token == second.token
            assert path.read_bytes() != stale
        finally:
            await second.close()

    asyncio.run(scenario())


def test_endpoint_does_not_reclaim_a_foreign_replacement_after_prior_ownership(tmp_path):
    async def handle(sid, message):
        return {"result": None}

    async def scenario():
        path = tmp_path / "private/control.json"
        first = ControlServer(path, handle)
        await first.start()
        await first.close()
        foreign = json.dumps(
            {"url": "http://127.0.0.1:12345", "token": "different-owner" * 4}
        ).encode()
        path.write_bytes(foreign)
        second = ControlServer(path, handle)
        with pytest.raises(ValueError, match="归属"):
            await second.start()
        await second.close()
        assert path.read_bytes() == foreign

    asyncio.run(scenario())


def test_endpoint_shutdown_preserves_a_replaced_descriptor(tmp_path):
    async def handle(sid, message):
        return {"result": None}

    async def scenario():
        path = tmp_path / "private/control.json"
        server = ControlServer(path, handle)
        await server.start()
        replacement = b"owned by another tool"
        path.write_bytes(replacement)
        await server.close()
        assert path.read_bytes() == replacement

    asyncio.run(scenario())


def test_endpoint_publication_failure_keeps_ownership_of_previous_descriptor(tmp_path, monkeypatch):
    from maa_planner.daemon import transport

    async def handle(sid, message):
        return {"result": None}

    async def scenario():
        path = tmp_path / "private/control.json"
        first = ControlServer(path, handle)
        await first.start()
        previous = path.read_bytes()
        await first.close()
        path.write_bytes(previous)
        publish = transport.write_private

        def interrupted(destination, content):
            if destination == path:
                raise OSError("publication interrupted")
            return publish(destination, content)

        with monkeypatch.context() as patch:
            patch.setattr(transport, "write_private", interrupted)
            with pytest.raises(OSError, match="interrupted"):
                await ControlServer(path, handle).start()
        assert path.read_bytes() == previous
        final = ControlServer(path, handle)
        await final.start()
        try:
            assert read_endpoint(path).token == final.token
        finally:
            await final.close()

    asyncio.run(scenario())
