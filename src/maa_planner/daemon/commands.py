"""Internal operation commands; never accepted directly by the control endpoint."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from maa_planner.daemon.copilot_command import CopilotCommand

ACTIONS = frozenset(
    {
        "copilot",
        "run",
        "activities",
        "news",
        "assets",
        "player-context",
        "plan",
        "emulators",
        "runtime-check",
        "resource-check",
        "resource-update",
        "game-check",
        "game-update",
        "runtime-prepare",
        "recover",
    }
)
MUTATING = frozenset(
    {
        "resource-update",
        "game-update",
        "runtime-prepare",
        "recover",
        "runtime-check",
        "resource-check",
    }
)


@dataclass(frozen=True)
class Submission:
    request_id: str
    action: str
    target: str | None
    arguments: dict
    expires_at: float | None

    @classmethod
    def from_dict(cls, params: dict) -> Submission:
        if set(params) - {"request_id", "action", "target", "arguments", "expires_at"}:
            raise ValueError("submit 包含未知字段")
        identifier, action = params.get("request_id"), params.get("action")
        target, arguments = params.get("target"), params.get("arguments", {})
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            raise ValueError("request_id 必须为 1～80 位字母、数字、下划线或连字符")
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(arguments, dict):
            raise ValueError("未知 action 或 arguments 不是对象")
        if target is not None and (not isinstance(target, str) or not target):
            raise ValueError("target 必须为账号名称")
        if target is not None and action not in {
            "copilot",
            "run",
            "player-context",
            "plan",
            "emulators",
            "game-check",
            "game-update",
            "recover",
        }:
            raise ValueError("此 action 为全局操作，不接受 target")
        if action == "plan" and (
            set(arguments) != {"schedule_id"}
            or not isinstance(arguments.get("schedule_id"), str)
            or not arguments["schedule_id"]
        ):
            raise ValueError("plan 必须通过 schedule_id 指定日程")
        if action == "assets":
            from maa_planner.schema.assets import validate_assets

            validate_assets(arguments)
        if action == "news":
            from maa_planner.schema.news import validate_budget

            validate_budget(arguments)
        if action not in {"copilot", "run", "plan", "news", "assets"} and arguments:
            raise ValueError("此 action 不接受 arguments；维护路径只从配置读取")
        if action in {"copilot", "run", "player-context", "plan", "recover"} and target is None:
            raise ValueError("此 action 必须指定 target")
        arguments = json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
        deadline = params.get("expires_at")
        if deadline is not None and (
            type(deadline) not in {float, int} or not math.isfinite(deadline)
        ):
            raise ValueError("expires_at 必须为有限 Unix 时间")
        if action == "copilot":
            if "request_id" in arguments or "profile" in arguments:
                raise ValueError("arguments 不允许覆盖 request_id 或 target")
            normalized = CopilotCommand.from_dict(
                {"request_id": identifier, "profile": target, **arguments}
            ).to_dict()
            arguments = {
                key: value
                for key, value in normalized.items()
                if key not in {"request_id", "profile"}
            }
        return cls(identifier, action, target, arguments, deadline)


@dataclass(frozen=True)
class BatchSubmission:
    request_id: str
    jobs: tuple[Submission, ...]

    @classmethod
    def from_dict(cls, params: dict) -> BatchSubmission:
        if (
            set(params) != {"request_id", "jobs"}
            or not isinstance(params["jobs"], list)
            or not 1 <= len(params["jobs"]) <= 100
        ):
            raise ValueError("batch.submit 需要 request_id 和 1～100 个 jobs")
        identifier = params["request_id"]
        # Reuse the public identifier validation; child IDs are assigned separately by the daemon.
        Submission.from_dict({"request_id": identifier, "action": "activities"})
        jobs = []
        targets = set()
        for index, job in enumerate(params["jobs"]):
            if not isinstance(job, dict) or "request_id" in job:
                raise ValueError("批次成员不接受 request_id")
            submission = Submission.from_dict({"request_id": str(index), **job})
            if submission.action not in {"run", "copilot"}:
                raise ValueError("批次只接受 run 或 copilot")
            if submission.target in targets:
                raise ValueError("同一批次不能重复指定账号")
            targets.add(submission.target)
            jobs.append(submission)
        return cls(identifier, tuple(jobs))
