import json
import threading

import pytest

from maa_planner.daemon.commands import Submission
from maa_planner.daemon.tools.news import collect_news, parse_article

BUDGET = {"max_pages": 2, "max_articles": 10, "failure_backoff": 60}


def article(identifier, body="<p>预计开放，不代表实际开放</p>"):
    value = {"cid": identifier, "title": "预告", "data": body, "displayTime": 123}
    # Split the serialized flight stream across multiple script tags, as Next.js
    # may do for large article payloads.
    text = '1:["$","client",null,{"articleData":' + json.dumps(value) + "}]"
    half = len(text) // 2
    return "".join(
        "<script>self.__next_f.push(" + json.dumps([1, part]) + ")</script>"
        for part in (text[:half], text[half:])
    ).encode()


def test_full_catchup_dedup_body_revisions_and_conditional_cache(tmp_path):
    now = [1000]
    calls = []
    revised = [False]

    def get(url, headers):
        calls.append((url, headers))
        if "/api/" in url:
            page = int(url.rsplit("=", 1)[1])
            return (
                200,
                {},
                json.dumps(
                    {
                        "code": 0,
                        "data": {"list": [{"cid": str(page)}, {"cid": "1"}], "end": page == 2},
                    }
                ).encode(),
            )
        identifier = url.rsplit("/", 1)[1]
        if headers.get("If-None-Match") == "v1" and not revised[0]:
            return 304, {}, b""
        return (
            200,
            {"ETag": "v2" if revised[0] else "v1"},
            article(identifier, "<p>修订内容</p>" if revised[0] else "<p>原文</p>"),
        )

    def scan():
        return collect_news(tmp_path, BUDGET, threading.Event(), get=get, clock=lambda: now[0])

    first = scan()
    assert first["scan_complete"] and first["articles_checked"] == 2
    assert first["discovered_ids"] == ["1", "2"]
    assert first["changed_ids"] == ["1", "2"]
    now[0] = 1100
    second = scan()
    assert not second["changed_ids"]
    assert second["articles"][0]["first_discovered_at"] == 1000
    assert second["articles"][0]["published_at"] == 123
    assert second["articles"][0]["fetched_at"] == 1000
    assert second["articles"][0]["checked_at"] == 1100
    revised[0] = True
    now[0] = 1200
    third = scan()
    assert third["changed_ids"] == ["1", "2"]
    index = json.loads((tmp_path / "official-news/index.json").read_text())
    assert len(index["articles"]["1"]["revisions"]) == 2
    assert len(list((tmp_path / "official-news/revisions").glob("*.json"))) == 4
    assert any(headers.get("If-None-Match") == "v1" for _, headers in calls)


def test_budgets_never_claim_complete_and_failure_backoff_preserves_evidence(tmp_path):
    now = [1000]
    fail = [False]
    calls = []

    def get(url, headers):
        calls.append(url)
        if fail[0]:
            raise OSError("source offline")
        return (
            200,
            {},
            json.dumps({"code": 0, "data": {"list": [{"cid": "1"}], "end": False}}).encode()
            if "/api/" in url
            else article("1"),
        )

    budget = {**BUDGET, "max_pages": 1}
    first = collect_news(tmp_path, budget, threading.Event(), get=get, clock=lambda: now[0])
    assert not first["scan_complete"] and not first["pagination_complete"]
    fail[0] = True
    with pytest.raises(OSError):
        collect_news(tmp_path, budget, threading.Event(), get=get, clock=lambda: now[0])
    count = len(calls)
    with pytest.raises(ValueError, match="退避"):
        collect_news(tmp_path, budget, threading.Event(), get=get, clock=lambda: now[0])
    assert len(calls) == count
    assert json.loads((tmp_path / "official-news/index.json").read_text())["articles"]["1"][
        "revisions"
    ]


def test_invalid_html_and_unbounded_news_commands_rejected():
    with pytest.raises(ValueError, match="结构变化"):
        parse_article(b"<html>navigation only</html>", "1")
    with pytest.raises(ValueError):
        Submission.from_dict({"request_id": "news", "action": "news"})
    with pytest.raises(ValueError):
        Submission.from_dict(
            {"request_id": "news", "action": "news", "arguments": {**BUDGET, "max_pages": True}}
        )
    assert (
        Submission.from_dict({"request_id": "news", "action": "news", "arguments": BUDGET}).target
        is None
    )


def test_api_news_scan_uses_only_explicit_cache_and_no_account(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace

    from maa_planner.daemon.config import settings_from_bytes
    from maa_planner.daemon.coordinator import Daemon

    calls = []

    def collect(cache, budget, cancel):
        calls.append((cache, budget, cancel.is_set()))
        return {"scan_complete": True}

    monkeypatch.setattr("maa_planner.daemon.tools.news.collect_news", collect)
    result = asyncio.run(
        Daemon(tmp_path / "config.toml", tmp_path / "control.json").observations.execute(
            "news",
            replace(
                settings_from_bytes(b'[[profiles]]\nname="a"', tmp_path / "config.toml"),
                cache_directory=tmp_path,
            ),
            threading.Event(),
            BUDGET,
        )
    )
    assert result == {"scan_complete": True}
    assert calls == [(tmp_path, BUDGET, False)]


def test_cancel_keeps_discovered_ids_without_attempting_body_or_applying_backoff(tmp_path):
    cancel = threading.Event()
    calls = []

    def get(url, headers):
        calls.append(url)
        # Cancellation between pages happens before the next request; no body
        # fetch is admitted and durable discovery from page one remains.
        if len(calls) > 1:
            cancel.set()
        return (
            200,
            {},
            json.dumps({"code": 0, "data": {"list": [{"cid": "1"}], "end": False}}).encode(),
        )

    with pytest.raises(InterruptedError):
        collect_news(tmp_path, BUDGET, cancel, get=get, clock=lambda: 1000)
    state = json.loads((tmp_path / "official-news/index.json").read_text())
    assert state["articles"]["1"]["first_discovered_at"] == 1000
    assert not state.get("retry_at")
    assert all("/api/" in url for url in calls)


def test_small_body_budget_eventually_catches_up_then_revisits_oldest(tmp_path):
    now = [1000]
    bodies = []

    def get(url, headers):
        if "/api/" in url:
            return (
                200,
                {},
                json.dumps(
                    {
                        "code": 0,
                        "data": {"list": [{"cid": str(n)} for n in range(1, 4)], "end": True},
                    }
                ).encode(),
            )
        identifier = url.rsplit("/", 1)[1]
        bodies.append(identifier)
        return 200, {}, article(identifier)

    for _ in range(4):
        collect_news(
            tmp_path,
            {**BUDGET, "max_articles": 1},
            threading.Event(),
            get=get,
            clock=lambda: now[0],
        )
        now[0] += 1
    assert bodies == ["1", "2", "3", "1"]
