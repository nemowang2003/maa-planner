import ssl
import threading

import httpx
import pytest

from maa_planner.daemon.execution import copilot


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(copilot, "wait_exponential", lambda **kwargs: lambda state: 0)


def test_public_post_recovers_from_tls_eof(tmp_path, monkeypatch):
    calls = []

    def handle(transport, request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("TLS interrupted", request=request) from ssl.SSLEOFError(
                "UNEXPECTED_EOF_WHILE_READING"
            )
        return httpx.Response(200, json={"status_code": 200, "data": {"data": []}})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    source = copilot._Source(threading.Event(), tmp_path)
    assert source.post("https://prts.maa.plus/set/query", {"page": 1}) == {"data": []}
    assert len(calls) == 2
    assert calls[0].content == calls[1].content
    assert source.requests == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_status_is_retried(status, tmp_path, monkeypatch):
    calls = []

    def handle(transport, request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status)
        return httpx.Response(200, json={"status_code": 200, "data": {}})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    assert copilot._Source(threading.Event(), tmp_path).get("https://public.test/job") == {}
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["tls", "timeout", "status"])
def test_retry_exhaustion_is_bounded_and_logs_url_and_traceback(
    failure, tmp_path, monkeypatch, caplog
):
    calls = []

    def handle(transport, request):
        calls.append(request)
        if failure == "tls":
            raise httpx.ConnectError("TLS interrupted", request=request) from ssl.SSLEOFError()
        if failure == "timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(503)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    source = copilot._Source(threading.Event(), tmp_path)
    with pytest.raises(OSError, match="https://public.test/job"):
        source.get("https://public.test/job")
    assert len(calls) == source.requests == 3
    assert not list(tmp_path.glob("*.json"))
    assert any("https://public.test/job" in r.message and r.exc_info for r in caplog.records)


@pytest.mark.parametrize("failure", ["certificate", "forbidden", "json", "redirect", "oversize"])
def test_permanent_errors_are_not_retried(failure, tmp_path, monkeypatch):
    calls = []

    def handle(transport, request):
        calls.append(request)
        if failure == "certificate":
            raise httpx.ConnectError(
                "certificate invalid", request=request
            ) from ssl.SSLCertVerificationError()
        if failure == "forbidden":
            return httpx.Response(403)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.test/"})
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * (copilot.MAX_RESPONSE + 1))
        return httpx.Response(200, content=b"invalid json")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    with pytest.raises((OSError, ValueError)):
        copilot._Source(threading.Event(), tmp_path).get("https://public.test/job")
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.json"))


def test_interrupted_response_body_is_retried_and_closed(tmp_path, monkeypatch):
    calls = []
    closed = []

    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"status_code":'
            raise httpx.ReadError("connection reset")

        def close(self):
            closed.append(True)

    def handle(transport, request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, stream=BrokenBody())
        return httpx.Response(200, json={"status_code": 200, "data": {"ok": True}})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    assert copilot._Source(threading.Event(), tmp_path).get("https://public.test/job") == {
        "ok": True
    }
    assert len(calls) == 2
    assert closed == [True]


def test_cancel_during_backoff_prevents_next_request(tmp_path, monkeypatch):
    cancel = threading.Event()
    calls = []

    def handle(transport, request):
        calls.append(request)
        cancel.set()
        raise httpx.ConnectError("interrupted", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    with pytest.raises(RuntimeError, match="取消"):
        copilot._Source(cancel, tmp_path).get("https://public.test/job")
    assert len(calls) == 1


def test_retries_do_not_exceed_request_budget(tmp_path, monkeypatch):
    calls = []

    def handle(transport, request):
        calls.append(request)
        raise httpx.ConnectError("interrupted", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    source = copilot._Source(threading.Event(), tmp_path)
    source.requests = copilot.MAX_REQUESTS - 1
    with pytest.raises(ValueError, match="请求次数限制"):
        source.get("https://public.test/job")
    assert source.requests == copilot.MAX_REQUESTS
    assert len(calls) == 1
