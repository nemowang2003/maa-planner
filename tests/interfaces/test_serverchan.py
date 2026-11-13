import asyncio
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from platformdirs import windows
from platformdirs.unix import Unix
from platformdirs.windows import Windows

from maa_planner.daemon.transport import ControlServer
from maa_planner.interfaces.serverchan import config as config_module
from maa_planner.interfaces.serverchan.client import ServerChanClient, post
from maa_planner.interfaces.serverchan.config import ServerChanConfig, validate_url
from tests.helpers.async_tools import eventually

URL = "https://123.push.ft07.com/send/sctp123tPRIVATE_KEY.send"


def test_admin_channel_only_uses_its_explicit_serverchan_route(tmp_path):
    client = ServerChanClient(
        ServerChanConfig({"admin": ("admin-url",), "nemo": ("account-url",)}),
        tmp_path / "unused",
    )
    client.receive({"kind": "alert.raised", "profile": "admin", "data": {}})
    assert client.queues["account-url"].empty()
    assert "admin" in client.queues["admin-url"].get_nowait()["title"]


@pytest.mark.parametrize("url", [URL, URL.removesuffix(".send")])
def test_full_sc3_urls_are_kept_without_sdk_rewriting(tmp_path, url):
    path = tmp_path / "serverchan.json"
    path.write_text(json.dumps({"nemo": [url], "disabled": []}))
    config = ServerChanConfig.load(path)
    assert config.routes == {"nemo": (url,), "disabled": ()}
    assert "PRIVATE_KEY" not in repr(config)


@pytest.mark.parametrize(
    "url",
    [
        "http://123.push.ft07.com/send/sctp123tPRIVATE_KEY.send",
        "https://sctapi.ftqq.com/SCTPRIVATE_KEY.send",
        "https://123.push.ft07.com.evil.example/send/sctp123tPRIVATE_KEY.send",
        "https://456.push.ft07.com/send/sctp123tPRIVATE_KEY.send",
        "https://u:p@123.push.ft07.com/send/sctp123tPRIVATE_KEY.send",
        URL + "?title=private",
        URL + "#fragment",
        "https://[PRIVATE_KEY",
        123,
        None,
    ],
)
def test_bad_urls_do_not_expose_sendkeys(url):
    with pytest.raises(ValueError) as error:
        validate_url(url)
    assert "PRIVATE_KEY" not in str(error.value)


@pytest.mark.parametrize("value", [[], {"nemo": URL}, {"nemo": [URL, URL]}, {"": []}])
def test_invalid_routes_are_rejected_without_printing_credentials(tmp_path, value):
    path = tmp_path / "serverchan.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError) as error:
        ServerChanConfig.load(path)
    assert "PRIVATE_KEY" not in str(error.value)


@pytest.mark.parametrize("platform,variable", [(Unix, "XDG_CONFIG_HOME"), (Windows, "APPDATA")])
def test_separate_config_path_uses_native_user_configuration(
    tmp_path, monkeypatch, platform, variable
):
    monkeypatch.setenv(variable, str(tmp_path))
    monkeypatch.setattr(windows, "get_win_folder", lambda _: str(tmp_path))
    monkeypatch.setattr(
        config_module, "user_config_path", lambda name, **kw: platform(name, **kw).user_config_path
    )
    assert (
        config_module.default_config_file()
        == tmp_path / "maa-planner-serverchan-client/serverchan.json"
    )


@asynccontextmanager
async def http_server(handler):
    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    "result,status,accepted",
    [
        ({"code": 0}, 200, True),
        ({"code": 1, "message": "PRIVATE_KEY"}, 200, False),
        ({"code": False}, 200, False),
        ({}, 200, False),
        ([], 200, False),
        ({"code": 0}, 500, False),
    ],
)
def test_json_post_checks_http_and_sc3_business_result(result, status, accepted):
    async def scenario():
        received = []

        async def handler(request):
            received.append(await request.json())
            assert request.content_type == "application/json"
            return web.json_response(result, status=status)

        async with http_server(handler) as url, aiohttp.ClientSession() as session:
            if accepted:
                await post(session, url, {"title": "账号：nemo", "desp": "中文消息🙂"})
            else:
                with pytest.raises(ValueError) as error:
                    await post(session, url, {"title": "账号：nemo", "desp": "中文消息🙂"})
                assert "PRIVATE_KEY" not in str(error.value)
            assert received == [{"title": "账号：nemo", "desp": "中文消息🙂"}]

    asyncio.run(scenario())


def test_redirect_is_not_followed_and_fragmented_json_is_read_completely():
    async def scenario():
        seen = []

        async def handler(request):
            seen.append(request.path)
            if request.path == "/redirect":
                return web.Response(status=302, headers={"Location": "/leak"})
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b'{"code":')
            await asyncio.sleep(0.01)
            await response.write(b"0}")
            await response.write_eof()
            return response

        async with http_server(handler) as url, aiohttp.ClientSession() as session:
            with pytest.raises(ValueError):
                await post(session, url + "/redirect", {"title": "test", "desp": "test"})
            await post(session, url + "/fragmented", {"title": "test", "desp": "test"})
            assert seen == ["/redirect", "/fragmented"]

    asyncio.run(scenario())


def test_passive_client_routes_every_notification_without_requests_or_blocking_other_urls(
    tmp_path, caplog
):
    async def scenario():
        received, commands = [], []
        slow = asyncio.Event()

        async def handler(request):
            message = await request.json()
            if "slow" in request.path:
                await slow.wait()
            received.append((request.path, message))
            return web.json_response(
                {"code": 1, "message": "PRIVATE_KEY"} if "bad" in request.path else {"code": 0}
            )

        async def control(sid, message):
            commands.append(message)
            raise AssertionError("receive-only client must not send daemon commands")

        server = ControlServer(tmp_path / "control.json", control)
        await server.start()
        stop = asyncio.Event()
        task = None
        try:
            async with http_server(handler) as url:
                # Local URLs are injected directly into this test, never loaded as production configuration.
                config = ServerChanConfig(
                    {
                        "nemo": (url + "/slow", url + "/bad-PRIVATE_KEY", url + "/fast"),
                        "wyf": (url + "/other",),
                    }
                )
                client = ServerChanClient(config, server.path)
                task = asyncio.create_task(client.run(stop))
                await eventually(lambda: bool(server.sessions))
                for kind in ("alert.raised", "alert.resolved"):
                    server.notifications.publish(
                        {
                            "kind": kind,
                            "profile": "nemo",
                            "message": "材料不足",
                            "data": {"item": "中文"},
                        }
                    )
                server.notifications.publish({"kind": "custom", "profile": "wyf", "data": {}})
                server.notifications.publish(
                    {"kind": "alert.raised", "profile": "unmapped", "data": {}}
                )
                server.notifications.publish({"kind": "alert.raised", "profile": None, "data": {}})
                await eventually(lambda: len(received) == 5)
                assert not any(path == "/slow" for path, _ in received)
                assert [
                    json.loads(body["desp"])["kind"] for path, body in received if path == "/fast"
                ] == ["alert.raised", "alert.resolved"]
                assert all("nemo" in body["title"] for path, body in received if path != "/other")
                assert not commands
                slow.set()
                await eventually(lambda: len(received) == 7)
                stop.set()
                await asyncio.wait_for(task, 2)
                assert len(received) == 7  # Rejected POSTs weren't retried.
                assert "PRIVATE_KEY" not in caplog.text
        finally:
            slow.set()
            stop.set()
            if task is not None:
                await asyncio.wait_for(task, 2)
            await server.close()

    asyncio.run(scenario())
