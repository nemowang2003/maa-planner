"""Explicit, bounded official news discovery; never establishes game eligibility."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from maa_planner.daemon.tools.locks import acquire, data_resource
from maa_planner.daemon.tools.storage import atomic_write, write_json
from maa_planner.schema.news import validate_budget

ORIGIN = "https://ak.hypergryph.com"
MAX_RESPONSE = 2 * 1024 * 1024


def fetch(url, headers):
    request = urllib.request.Request(url, headers={"User-Agent": "maa-planner", **headers})
    try:
        response = urllib.request.urlopen(request, timeout=20)
    except urllib.error.HTTPError as error:
        if error.code != 304:
            raise
        return 304, dict(error.headers.items()), b""
    with response:
        if response.geturl().split("/", 3)[:3] != ORIGIN.split("/")[:3]:
            raise ValueError("新闻来源重定向离开官网")
        content = response.read(MAX_RESPONSE + 1)
        if len(content) > MAX_RESPONSE:
            raise ValueError("新闻响应超过大小限制")
        return response.status, dict(response.headers.items()), content


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.scripts = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            self.current = []

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.scripts.append("".join(self.current))
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.current.append(data)


def parse_article(content: bytes, identifier: str) -> dict:
    parser = ArticleParser()
    parser.feed(content.decode("utf-8"))
    flight = []
    for script in parser.scripts:
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\)", script, re.S)
        if match:
            try:
                payload = json.loads(match[1])
            except ValueError:
                continue
            if (
                isinstance(payload, list)
                and len(payload) > 1
                and payload[0] == 1
                and isinstance(payload[1], str)
            ):
                flight.append(payload[1])
    text = "".join(flight)
    articles = []
    for match in re.finditer(r'"articleData"\s*:\s*', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end() :])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("cid") == identifier and value not in articles:
            articles.append(value)
    if len(articles) != 1:
        raise ValueError("官网正文结构变化或活动 ID 不匹配，不能将网页壳视为正文")
    article = articles[0]
    if (
        any(not isinstance(article.get(k), str) or not article[k] for k in ("cid", "title", "data"))
        or type(article.get("displayTime")) is not int
    ):
        raise ValueError("官网正文缺少标题、内容或发布时间")
    return article


def collect_news(
    cache: Path, arguments: dict, cancel: threading.Event, *, get=fetch, clock=time.time
):
    budget = validate_budget(arguments)
    root = cache / "official-news"
    root.mkdir(parents=True, exist_ok=True)
    with acquire([data_resource(root)]):
        path = root / "index.json"
        state: dict = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"documents": {}, "articles": {}}
        )
        if clock() < state.get("retry_at", 0):
            raise ValueError(f"官网采集处于失败退避中，下次允许时间 {state['retry_at']}")
        (root / "responses").mkdir(exist_ok=True)
        (root / "revisions").mkdir(exist_ok=True)

        def check_cancel():
            if cancel.is_set():
                raise InterruptedError("官网采集已取消")

        def document(url):
            check_cancel()
            old = state["documents"].get(url, {})
            headers = {}
            if old.get("etag"):
                headers["If-None-Match"] = old["etag"]
            if old.get("modified"):
                headers["If-Modified-Since"] = old["modified"]
            code, response_headers, content = get(url, headers)
            check_cancel()
            if code == 304:
                if not old:
                    raise ValueError("官网返回 304 但本地没有来源快照")
                content = (root / "responses" / (old["sha256"] + ".bin")).read_bytes()
                if hashlib.sha256(content).hexdigest() != old["sha256"]:
                    raise ValueError("官网缓存校验失败")
                metadata = {**old, "checked_at": clock()}
            elif code == 200:
                if not isinstance(content, bytes) or len(content) > MAX_RESPONSE:
                    raise ValueError("新闻响应无效或过大")
                sha = hashlib.sha256(content).hexdigest()
                destination = root / "responses" / (sha + ".bin")
                if not destination.exists():
                    atomic_write(destination, content)
                normalized = {k.lower(): v for k, v in response_headers.items()}
                metadata = {
                    "sha256": sha,
                    "fetched_at": clock(),
                    "checked_at": clock(),
                    "etag": normalized.get("etag"),
                    "modified": normalized.get("last-modified"),
                }
            else:
                raise ValueError(f"官网响应状态 {code}")
            state["documents"][url] = metadata
            return content, metadata

        changed, discovered, seen, fingerprints = [], [], [], set()
        pagination_complete = False
        try:
            for page in range(1, budget["max_pages"] + 1):
                content, _ = document(f"{ORIGIN}/api/news?page={page}")
                value = json.loads(content)
                data = value.get("data", {}) if isinstance(value, dict) else {}
                rows = data.get("list") if isinstance(data, dict) else None
                if (
                    not isinstance(value, dict)
                    or value.get("code") != 0
                    or not isinstance(rows, list)
                    or type(data.get("end")) is not bool
                ):
                    raise ValueError("官网新闻分页格式变化")
                page_ids = []
                for row in rows:
                    identifier = row.get("cid") if isinstance(row, dict) else None
                    if not isinstance(identifier, str) or not re.fullmatch(
                        r"[0-9]{1,32}", identifier
                    ):
                        raise ValueError("官网新闻 ID 无效")
                    page_ids.append(identifier)
                    if identifier not in seen:
                        seen.append(identifier)
                    if identifier not in state["articles"]:
                        state["articles"][identifier] = {
                            "first_discovered_at": clock(),
                            "revisions": [],
                        }
                        discovered.append(identifier)
                    state["articles"][identifier]["listing"] = row
                fingerprint = tuple(page_ids)
                if fingerprint in fingerprints and not data["end"]:
                    raise ValueError("官网重复返回同一分页，停止无限追赶")
                fingerprints.add(fingerprint)
                if data["end"]:
                    pagination_complete = True
                    break
            pending = seen + [
                identifier for identifier in state["articles"] if identifier not in seen
            ]
            # Continue bounded scans fairly: fetch undiscovered bodies first,
            # then recheck the oldest body instead of starving later IDs behind
            # a small max_articles budget on every call. Stable sort preserves
            # official page order when timestamps tie.
            pending.sort(key=lambda identifier: state["articles"][identifier].get("checked_at", 0))
            checked = []
            for identifier in pending[: budget["max_articles"]]:
                content, metadata = document(f"{ORIGIN}/news/{identifier}")
                article = parse_article(content, identifier)
                encoded = json.dumps(article, ensure_ascii=False, sort_keys=True).encode()
                sha = hashlib.sha256(encoded).hexdigest()
                destination = root / "revisions" / (sha + ".json")
                if not destination.exists():
                    atomic_write(destination, encoded)
                record = state["articles"][identifier]
                revision = {
                    "sha256": sha,
                    "first_fetched_at": clock(),
                    "source_sha256": metadata["sha256"],
                }
                if not record["revisions"] or record["revisions"][-1]["sha256"] != sha:
                    record["revisions"].append(revision)
                    changed.append(identifier)
                record.update(
                    {
                        "url": f"{ORIGIN}/news/{identifier}",
                        "published_at": article["displayTime"],
                        "title": article["title"],
                        "current_revision": sha,
                        "fetched_at": metadata["fetched_at"],
                        "checked_at": metadata["checked_at"],
                        "source_sha256": metadata["sha256"],
                    }
                )
                checked.append(identifier)
            state.update({"last_scan_at": clock(), "retry_at": 0, "last_error": None})
            write_json(path, state)
            return {
                "source": ORIGIN,
                "pagination_complete": pagination_complete,
                "scan_complete": pagination_complete and len(checked) == len(pending),
                "pages_checked": page,
                "articles_checked": len(checked),
                "pending_ids": pending[len(checked) :],
                "discovered_ids": discovered,
                "changed_ids": changed,
                "index": str(path),
                "articles": [
                    {
                        "id": identifier,
                        **{
                            k: v
                            for k, v in state["articles"][identifier].items()
                            if k not in {"listing", "revisions"}
                        },
                    }
                    for identifier in checked
                ],
            }
        except InterruptedError:
            write_json(path, state)
            raise
        except Exception as error:
            retry_at = clock() + budget["failure_backoff"]
            if isinstance(error, urllib.error.HTTPError):
                after = error.headers.get("Retry-After", "")
                try:
                    server_time = (
                        clock() + int(after)
                        if after.isdecimal()
                        else parsedate_to_datetime(after).timestamp()
                    )
                    retry_at = max(retry_at, server_time)
                except (ValueError, TypeError, OverflowError):
                    pass
            state.update({"retry_at": retry_at, "last_error": str(error)})
            write_json(path, state)
            raise
