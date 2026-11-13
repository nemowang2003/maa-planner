"""Explicit user assertions about one account's activity-stage completion."""

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class StageConfirmation:
    profile: str
    activity_id: str
    stage_id: str
    is_raid: bool = False

    @classmethod
    def from_dict(cls, params: dict) -> StageConfirmation:
        if set(params) - {"profile", "activity_id", "stage_id", "is_raid"}:
            raise ValueError("关卡确认字段无效")
        if not isinstance(params.get("profile"), str) or not params["profile"]:
            raise ValueError("关卡确认需要 profile")
        for key in ("activity_id", "stage_id"):
            value = params.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
                raise ValueError(f"关卡确认需要合法 {key}")
        if type(params.get("is_raid", False)) is not bool:
            raise ValueError("is_raid 必须为布尔值")
        return cls(**params)

    def to_dict(self) -> dict:
        return asdict(self)
