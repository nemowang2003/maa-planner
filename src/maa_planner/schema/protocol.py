"""Socket.IO application contracts. No daemon, interface or transport dependencies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict

MAX_MESSAGE = 1024 * 1024
MAX_PENDING_NOTIFICATIONS = 256
ADMIN_CHANNEL = "admin"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "blocked", "recovery_required"})


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str = field(repr=False)

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value) != {"url", "token"}
            or not isinstance(value["url"], str)
            or not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", value["url"])
            or int(value["url"].rsplit(":", 1)[1]) > 65535
            or not isinstance(value["token"], str)
            or len(value["token"]) < 32
        ):
            raise ValueError("无效的本机端点文件")
        return cls(**value)


@dataclass(frozen=True)
class Request:
    method: str
    params: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict) -> Request:
        if (
            not isinstance(value, dict)
            or set(value) - {"method", "params"}
            or not isinstance(value.get("method"), str)
            or not isinstance(value.get("params", {}), dict)
        ):
            raise ValueError("需要 method 字符串和 params 对象")
        return cls(value["method"], value.get("params", {}))

    def to_dict(self) -> dict:
        return {"method": self.method, "params": self.params}


class APIError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class Response:
    result: Any = None
    error: dict | None = None

    def to_dict(self) -> dict:
        return {
            **({"error": self.error} if self.error is not None else {"result": self.result}),
        }

    @classmethod
    def from_dict(cls, value: dict) -> Response:
        if (
            not isinstance(value, dict)
            or set(value) - {"result", "error"}
            or ("result" in value) == ("error" in value)
        ):
            raise ValueError("daemon 返回无效响应")
        error = value.get("error")
        if error is not None and (
            not isinstance(error, dict) or not isinstance(error.get("message"), str)
        ):
            raise ValueError("daemon 返回无效错误")
        return cls(value.get("result"), error)

    def unwrap(self):
        if self.error is not None:
            raise APIError(
                self.error.get("code", "unknown"), self.error["message"], self.error.get("details")
            )
        return self.result


@dataclass(frozen=True)
class ConfigurationUpdate:
    toml: str
    expected_revision: str | None = None

    @classmethod
    def from_dict(cls, params: dict) -> ConfigurationUpdate:
        if (
            set(params) - {"toml", "expected_revision"}
            or not isinstance(params.get("toml"), str)
            or (
                params.get("expected_revision") is not None
                and not isinstance(params["expected_revision"], str)
            )
        ):
            raise ValueError("需要完整 toml 字符串，可选 expected_revision 字符串")
        return cls(params["toml"], params.get("expected_revision"))


class Operation(TypedDict, total=False):
    operation_id: str
    method: str
    profile: str | None
    idempotency_key: str
    latest_start_at: float | None
    status: str
    result: Any
    error: str | None
    cancel_requested: bool
    configuration_revision: str
    runtime: dict | None
    attempt_id: str


class Alert(TypedDict):
    alert_id: str
    profile: str
    category: str
    subject: str
    active: bool
    raised_at: float
    updated_at: float
    resolved_at: float | None
    details: dict


class Event(TypedDict):
    sequence: int
    kind: str
    data: dict


class Notification(TypedDict):
    kind: str
    profile: str | None
    data: dict
    message: NotRequired[str]
