"""Pure validation for daily control protocol data."""

import math


def validate_daily(method, params):
    for key in ("not_before", "expires_at"):
        if key in params and (
            type(params[key]) not in (int, float) or not math.isfinite(params[key])
        ):
            raise ValueError(f"{key} 必须为有限 Unix 秒")
    for key in ("occurrence_id", "expected_revision", "schedule_id"):
        if key in params and (not isinstance(params[key], str) or not params[key]):
            raise ValueError(f"{key} 必须为非空字符串")
    if "expected_state_version" in params and (
        type(params["expected_state_version"]) is not int or params["expected_state_version"] < 0
    ):
        raise ValueError("expected_state_version 必须为非负整数")
    if method == "daily.run":
        if ("schedule_id" in params) == ("inline" in params):
            raise ValueError("schedule_id 与 inline 必须恰好提供一个")
        if "inline" in params and not isinstance(params["inline"], dict):
            raise ValueError("inline 必须为日常定义对象")

    if method == "daily.pause" and type(params.get("paused")) is not bool:
        raise ValueError("paused 必须为布尔值")
    if method == "daily.retry":
        values = params.get("task_ids")
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(v, str) or not v.startswith("task:") or not v[5:].isdigit()
                for v in values
            )
            or len(set(values)) != len(values)
        ):
            raise ValueError("task_ids 必须为不重复的业务任务标识列表")
