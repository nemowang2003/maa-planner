"""Internal fixed copilot execution command."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CopilotCommand:
    request_id: str
    profile: str
    url: str
    formation: bool = False
    ignore_requirements: bool = False
    start_at: int = 1
    select: tuple[int, ...] | None = None
    is_raid: bool = False
    use_sanity_potion: bool = False

    def __post_init__(self):
        if not isinstance(self.request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,80}", self.request_id
        ):
            raise ValueError("request_id 必须为 1～80 位字母、数字、下划线或连字符")
        if not isinstance(self.profile, str) or not self.profile:
            raise ValueError("必须指定目标 profile")
        if not isinstance(self.url, str) or not re.fullmatch(r"prts://s?[1-9]\d{0,18}", self.url):
            raise ValueError("作业 URL 必须为 prts://ID 或 prts://sID")
        if type(self.formation) is not bool or type(self.ignore_requirements) is not bool:
            raise ValueError("formation 和 ignore_requirements 必须为布尔值")
        if type(self.is_raid) is not bool:
            raise ValueError("is_raid 必须为布尔值")
        if type(self.use_sanity_potion) is not bool:
            raise ValueError("use_sanity_potion 必须为布尔值")
        if type(self.start_at) is not int or self.start_at < 1:
            raise ValueError("start_at 必须为正整数（作业集内从 1 开始的位置）")
        if self.select is not None:
            if (
                not isinstance(self.select, tuple)
                or not self.select
                or any(type(index) is not int or index < 1 for index in self.select)
            ):
                raise ValueError("select 必须为非空的正整数序号列表")
            if len(set(self.select)) != len(self.select):
                raise ValueError("select 不允许重复序号")
            if self.start_at != 1:
                raise ValueError("select 与 start_at 不能同时使用")
            if not self.url.startswith("prts://s"):
                raise ValueError("select 仅适用于作业集 URL")

    def to_dict(self) -> dict:
        value = asdict(self)
        # Omit unset optional parameters from the fixed operation payload.
        for option in ("is_raid", "use_sanity_potion"):
            if not value[option]:
                value.pop(option)
        if self.select is not None:
            value["select"] = list(self.select)
        return value

    @classmethod
    def from_dict(cls, value: dict) -> CopilotCommand:
        allowed = {
            "request_id",
            "profile",
            "url",
            "formation",
            "ignore_requirements",
            "start_at",
            "select",
            "is_raid",
            "use_sanity_potion",
        }
        if set(value) - allowed or not {"request_id", "profile", "url"} <= set(value):
            raise ValueError("copilot 请求字段缺失或包含未知字段")
        value = dict(value)
        if value.get("select") is not None:
            if not isinstance(value["select"], list):
                raise ValueError("select 必须为序号数组")
            value["select"] = tuple(value["select"])
        return cls(**value)
