"""Daemon-owned preparation of immediate, immutable online copilot requests."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import ssl
import threading
from pathlib import Path

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential
from tenacity.nap import sleep_using_event

from maa_planner.daemon.adapters.mumu import MuMu, match_profiles
from maa_planner.daemon.config import Settings
from maa_planner.daemon.constants import COPILOT_API_URL
from maa_planner.daemon.copilot_command import CopilotCommand
from maa_planner.daemon.execution.context import EmulatorRuntime, RunRequest, UnitBinding
from maa_planner.daemon.execution.runner import binding_resources
from maa_planner.daemon.execution.runtime import CoreRun
from maa_planner.daemon.execution.tasks import CoreTasks, json_object
from maa_planner.daemon.tools.locks import acquire, data_resource

MAX_RESPONSE = 4 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_SCRIPTS = 100
MAX_REQUESTS = 200
_LOGGER = logging.getLogger(__name__)


def _transient_request_error(error: BaseException) -> bool:
    cause = error
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return False
        cause = cause.__cause__
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(
        error, (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
    )


def resolve_binding(settings: Settings, name: str, require_game: bool = False) -> UnitBinding:
    if settings.mumu_path is None:
        raise ValueError("需要配置 [mumu]")
    profile = next((p for p in settings.profiles if p.name == name), None)
    if profile is None or not profile.enabled:
        raise ValueError(f"未知或已禁用的 profile: {name}")
    instance = match_profiles((profile,), MuMu(settings.mumu_path).instances())[profile]
    binding = UnitBinding(profile, settings.mumu_path, instance.index, instance.name)
    with acquire(binding_resources(binding)):
        runtime = EmulatorRuntime(
            allow_foreground_game=require_game,
            package_name=settings.runtime_template().game_package,
        )
        runtime.ensure_stopped(binding)
        if require_game:
            runtime.require_game_running(binding)
    return binding


def preflight_copilot(settings: Settings, command: CopilotCommand) -> UnitBinding:
    return resolve_binding(settings, command.profile, True)


class _Source:
    def __init__(self, cancel: threading.Event, cache: Path | None = None):
        self.cancel = cancel
        self.cache = cache
        self.total = 0
        self.requests = 0

    def get(self, url: str) -> dict:
        return self._request(url)

    def post(self, url: str, data: dict) -> dict:
        return self._request(url, data)

    def _request(self, url: str, data: dict | None = None) -> dict:
        if self.cancel.is_set():
            raise RuntimeError("copilot 准备已取消")
        if self.total >= MAX_TOTAL or self.requests >= MAX_REQUESTS:
            raise ValueError("作业下载超过总大小或请求次数限制")
        cached = None
        cache_path = (
            self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
            if self.cache and data is None
            else None
        )
        if cache_path is not None and cache_path.is_file():
            try:
                candidate = json_object(cache_path.read_text(encoding="utf-8"))
                if candidate.get("url") == url and isinstance(candidate.get("body"), str):
                    cached = candidate
            except (OSError, ValueError):
                pass
        headers = {"User-Agent": "maa-planner"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        method = "POST" if data is not None else "GET"

        def fetch(client):
            if self.cancel.is_set():
                raise RuntimeError("copilot 准备已取消")
            if self.requests >= MAX_REQUESTS:
                raise ValueError("作业下载超过请求次数限制")
            self.requests += 1
            with client.stream(
                method,
                url,
                headers=headers,
                content=json.dumps(data).encode() if data is not None else None,
            ) as response:
                if response.status_code == 304 and cached is not None:
                    return cached["body"].encode("utf-8"), cached.get("etag")
                if response.is_redirect:
                    raise ValueError("作业 API 意外重定向")
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if self.cancel.is_set():
                        raise RuntimeError("copilot 准备已取消")
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE or self.total + len(content) > MAX_TOTAL:
                        raise ValueError("作业下载超过大小限制")
                return bytes(content), response.headers.get("ETag")

        def log_retry(state):
            _LOGGER.warning(
                "作业 API 请求将重试: %s %s，尝试 %s/3: %s",
                method,
                url,
                state.attempt_number,
                state.outcome.exception(),
            )

        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                content, etag = Retrying(
                    retry=retry_if_exception(_transient_request_error),
                    stop=stop_after_attempt(3),
                    wait=wait_exponential(multiplier=0.5, max=2),
                    sleep=sleep_using_event(self.cancel),
                    before_sleep=log_retry,
                    reraise=True,
                )(fetch, client)
        except httpx.HTTPError as error:
            _LOGGER.exception("作业 API 请求失败: %s %s", method, url)
            # Keep source failures compatible with callers' candidate fallback.
            raise OSError(f"作业 API 请求失败: {method} {url}: {error}") from error
        self.total += len(content)
        if len(content) > MAX_RESPONSE or self.total > MAX_TOTAL:
            raise ValueError("作业下载超过大小限制")
        value = json_object(content.decode("utf-8"))
        if value.get("status_code") != 200 or not isinstance(value.get("data"), dict):
            raise ValueError("作业 API 返回失败或无效数据")
        if cache_path is not None:
            from maa_planner.daemon.tools.storage import write_json

            cache_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(cache_path, {"url": url, "etag": etag, "body": content.decode("utf-8")})
        return value["data"]


def download_scripts(
    settings: Settings,
    command: CopilotCommand,
    cancel: threading.Event,
) -> tuple[tuple[tuple[int, dict], ...], bool]:
    source = _Source(cancel, settings.cache_directory / "copilot")
    api_url = COPILOT_API_URL.rstrip("/")
    code = command.url.removeprefix("prts://")
    is_set = code.startswith("s")
    if is_set:
        ids = source.get(api_url + "/set/get?id=" + code[1:]).get("copilot_ids")
        if not isinstance(ids, list) or not ids or len(ids) > MAX_SCRIPTS:
            raise ValueError("作业集为空、格式错误或超过 100 份")
        if any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError("作业集包含无效 ID")
    else:
        ids = [int(code)]
    if command.start_at > len(ids):
        raise ValueError("start_at 超出作业集范围")
    if command.select is not None:
        if max(command.select) > len(ids):
            raise ValueError("select 序号超出作业集范围")
        selected = [
            identifier for index, identifier in enumerate(ids, 1) if index in command.select
        ]
    else:
        selected = ids[command.start_at - 1 :]
    scripts = []
    # Source reads stay outside runtime locks. Only inspect shared local resources
    # under a read lock after the downloads have finished.
    for identifier in selected:
        content = source.get(f"{api_url}/copilot/get/{identifier}").get("content")
        if not isinstance(content, str):
            raise ValueError("作业缺少 JSON content")
        script = json_object(content)
        stage = script.get("stage_name")
        if not isinstance(stage, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", stage):
            raise ValueError("作业缺少合法 stage_name")
        if not isinstance(script.get("actions"), list):
            raise ValueError("仅支持普通 Copilot 战斗作业")
        scripts.append((identifier, script))
    return tuple(scripts), is_set


def prepare_copilot(
    settings: Settings,
    command: CopilotCommand,
    binding: UnitBinding,
    revision: str,
    attempt_id: str,
    cancel: threading.Event,
) -> RunRequest:
    assert settings.core_runtime is not None
    scripts, is_set = download_scripts(settings, command, cancel)
    attachments = {f"copilot-{identifier}.json": script for identifier, script in scripts}
    stages = [
        (f"copilot-{identifier}.json", script["stage_name"]) for identifier, script in scripts
    ]
    runtime = settings.core_runtime
    with acquire(
        [
            data_resource(runtime.data_dir, shared=True),
            data_resource(runtime.resources, shared=True),
        ]
    ):
        queue = []
        for name, stage in stages:
            files = sorted((runtime.resources / "Arknights-Tile-Pos").glob(stage + "-*.json"))
            exact = runtime.resources / "Arknights-Tile-Pos" / (stage + ".json")
            if exact.is_file():
                files.append(exact)
            codes = {json_object(path.read_text(encoding="utf-8"))["code"] for path in files}
            if len(codes) != 1 or not all(isinstance(code, str) and code for code in codes):
                raise ValueError(f"资源中无法唯一确定关卡 {stage}，请先更新资源")
            queue.append(
                {
                    "id": len(queue),
                    "filename": "@file:" + name,
                    "nav_name_override": codes.pop(),
                    "is_raid": command.is_raid,
                }
            )
    params: dict[str, object] = {
        "formation": command.formation,
        "ignore_requirements": command.ignore_requirements,
        "use_sanity_potion": command.use_sanity_potion,
    }
    # Core only switches raid difficulty through its navigable list interface.
    # Ordinary single scripts retain their existing stage-entry semantics.
    if is_set or command.is_raid:
        params["copilot_list"] = queue
    else:
        params["filename"] = queue[0]["filename"]
        params["loop_times"] = 1
    tasks = CoreTasks(
        json.dumps({"tasks": [{"name": command.url, "type": "Copilot", "params": params}]}),
        json.dumps(attachments, ensure_ascii=False),
    )
    if cancel.is_set():
        raise RuntimeError("copilot 准备已取消")
    return RunRequest(
        attempt_id,
        attempt_id,
        revision,
        binding,
        core=CoreRun(
            runtime,
            tasks,
            json.dumps(
                {
                    "instance_options": {"touch_mode": runtime.touch_mode}
                    if runtime.touch_mode is not None
                    else {}
                }
            ),
        ),
    )
