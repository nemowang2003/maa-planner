"""Explicit text commands mapped only to the public daemon contract."""

from __future__ import annotations

import hashlib
import json

from maa_planner.schema import METHODS, Request

HELP = """maa-planner 命令：
/help — 帮助
/status — daemon 状态
/alerts [账号名] — 当前告警
/get 操作ID — 操作状态与结果
/cancel 操作ID — 请求取消操作
/api {"method":"...","params":{...}} — 调用完整 daemon API

执行与维护通过 /api 显式提交。超时不会自动重发，也不表示操作未执行。"""


def parse_command(text: str, *, bot_id: str, message_id: str) -> Request | None:
    text = text.strip()
    command, _, argument = text.partition(" ")
    argument = argument.strip()
    if command == "/help" and not argument:
        return None
    if command == "/status" and not argument:
        return Request("daemon.status")
    if command == "/alerts":
        return Request("alert.list", {"profile": argument} if argument else {})
    if command in {"/get", "/cancel"} and argument:
        return Request(
            "operation.get" if command == "/get" else "operation.cancel", {"operation_id": argument}
        )
    if command == "/api" and argument:
        try:
            value = json.loads(argument)
        except ValueError:
            raise ValueError("/api 后需要有效 JSON 对象") from None
        request = Request.from_dict(value)
        if (
            request.method in METHODS
            and METHODS[request.method].kind == "operation"
            and "idempotency_key" not in request.params
        ):
            digest = hashlib.sha256((bot_id + "\0" + message_id).encode()).hexdigest()
            request = Request(
                request.method, {**request.params, "idempotency_key": "wecom-" + digest}
            )
        return request
    raise ValueError("未知命令或参数不完整，请发送 /help")


def render_result(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_alert(notification: dict) -> str:
    data = notification.get("data", {})
    heading = "告警解除" if notification.get("kind") == "alert.resolved" else "告警"
    return (
        f"{heading} · 账号：{notification.get('profile') or '全局'}\n"
        f"{notification.get('message') or data.get('category', '')}\n" + render_result(data)
    )


def render_operation(operation: dict) -> str:
    return (
        f"操作 {operation['operation_id']}\n"
        f"账号：{operation.get('profile') or '全局'}\n"
        f"状态：{operation['status']}\n" + render_result(operation)
    )
