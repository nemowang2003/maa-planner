"""Configured APK maintenance clock and a login-free resource readiness probe."""

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from maa_planner.daemon.constants import BUSINESS_TIMEZONE
from maa_planner.daemon.planning.schedule import clock_minute

READY_NODE = "Planner@GameResources@Ready"
CHECK_NODE = "Planner@GameResources@Check"


@dataclass(frozen=True)
class GameUpdateSchedule:
    enabled: bool = True
    time: str = "11:00"
    poll_interval: int = 30
    resource_timeout: float = 3600

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or set(raw) - {
            "enabled",
            "time",
            "poll_interval",
            "resource_timeout",
        }:
            raise ValueError("[game_update] 包含未知字段")
        value = cls(**raw)
        try:
            clock_minute(value.time)
        except ValueError as error:
            raise ValueError("game_update.time 必须为北京时间 HH:MM") from error
        if type(value.enabled) is not bool:
            raise ValueError("game_update.enabled 必须为布尔值")
        if type(value.poll_interval) is not int or value.poll_interval not in (10, 30, 60):
            raise ValueError("game_update.poll_interval 必须为 10、30 或 60 秒")
        if (
            type(value.resource_timeout) not in (float, int)
            or not math.isfinite(value.resource_timeout)
            or value.resource_timeout <= 0
        ):
            raise ValueError("game_update.resource_timeout 必须为有限正秒数")
        return value

    def next_after(self, now, *, inclusive=False):
        local = datetime.fromtimestamp(now, BUSINESS_TIMEZONE)
        minute = clock_minute(self.time)
        moment = local.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        if moment.timestamp() < now or (not inclusive and moment.timestamp() == now):
            moment += timedelta(days=1)
        return moment.timestamp()


def readiness_tasks(poll_interval):
    from maa_planner.daemon.execution.tasks import CoreTasks

    def n(name):
        return "Planner@GameResources@" + name

    nodes = {
        CHECK_NODE: {
            "algorithm": "JustReturn",
            "next": [READY_NODE, n("DownloadPrompt"), n("Wait")],
        },
        READY_NODE: {
            "baseTask": "GameStart",
            "template": "GameStart.png",
            "action": "DoNothing",
            "next": [],
            "sub": [],
            "onErrorNext": [],
            "exceededNext": [],
            "reduceOtherTimes": [],
            "postDelay": 0,
        },
        n("Wait"): {
            "algorithm": "JustReturn",
            "action": "DoNothing",
            "postDelay": poll_interval * 1000,
            "maxTimes": 2**31 - 1,
            "next": [CHECK_NODE],
        },
        n("DownloadPrompt"): {
            "algorithm": "OcrDetect",
            "text": ["下载资源", "资源下载", "资源更新", "更新资源", "客户端资源"],
            "roi": [200, 150, 880, 350],
            "action": "DoNothing",
            "next": [n("ConfirmDownload"), n("Wait")],
        },
        n("ConfirmDownload"): {
            "algorithm": "OcrDetect",
            "text": ["确定", "确认", "开始下载", "立即下载"],
            "fullMatch": True,
            "roi": [550, 350, 650, 260],
            "action": "ClickSelf",
            "postDelay": 1000,
            "next": [n("Wait")],
        },
    }
    return CoreTasks(
        json.dumps(
            {
                "tasks": [
                    {
                        "type": "Custom",
                        "name": "等待黄色 START 资源就绪标记",
                        "params": {
                            "task_names": [CHECK_NODE],
                            "game_resource_ready": {"node": READY_NODE},
                        },
                    },
                    {
                        "type": "CloseDown",
                        "name": "资源就绪后关闭游戏",
                        "params": {"client_type": "Official"},
                    },
                ],
                "business_ids": ["maintenance:resources", "maintenance:close"],
                "resource_tasks": nodes,
            },
            ensure_ascii=False,
        )
    )


def validate_marker(task):
    marker = task.get("params", {}).get("game_resource_ready")
    if marker is None:
        return
    if (
        task["type"] != "Custom"
        or marker != {"node": READY_NODE}
        or task["params"].get("task_names") != [CHECK_NODE]
    ):
        raise ValueError("game_resource_ready 必须使用私有资源就绪检查任务")
