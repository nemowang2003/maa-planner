import json
import threading

import httpx
import pytest

from maa_planner.daemon.execution import copilot


def test_cached_copilot_is_revalidated_and_network_failure_is_not_silently_reused(
    tmp_path, monkeypatch
):
    url = "https://prts.maa.plus/copilot/get/123"
    body = json.dumps({"status_code": 200, "data": {"content": "original"}}).encode()

    calls = []

    def fetch(transport, request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, content=body, headers={"ETag": '"revision-1"'})
        assert request.headers["if-none-match"] == '"revision-1"'
        if len(calls) == 2:
            return httpx.Response(304)
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fetch)
    monkeypatch.setattr(copilot, "wait_exponential", lambda **kwargs: lambda state: 0)
    source = copilot._Source(threading.Event(), tmp_path)
    fixed = source.get(url)
    assert source.get(url) == fixed
    assert len(list(tmp_path.glob("*.json"))) == 1
    with pytest.raises(OSError, match="offline"):
        source.get(url)
    assert fixed == {"content": "original"}
