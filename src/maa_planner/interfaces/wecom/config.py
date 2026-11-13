"""Independent WeCom credentials, routing and native configuration paths."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_path

from maa_planner.schema import ADMIN_CHANNEL

APP_NAME = "maa-planner-wecom-client"


def default_config_file() -> Path:
    return user_config_path(APP_NAME, appauthor=False, roaming=True) / "wecom.json"


def identifiers(value, name: str) -> frozenset[str]:
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v.strip() or v != v.strip() for v in value
    ):
        raise ValueError(f"{name} 必须为非空字符串组成的列表")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} 不能有重复项")
    return frozenset(value)


@dataclass(frozen=True)
class WeComConfig:
    bot_id: str = field(repr=False)
    secret: str = field(repr=False)
    admins: frozenset[str]
    routes: dict[str, frozenset[str]]

    @classmethod
    def load(cls, path: Path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError("企业微信配置不是有效 JSON") from None
        if not isinstance(value, dict) or set(value) != {
            "bot_id",
            "secret",
            "admins",
            "routes",
        }:
            raise ValueError("配置必须且只能包含 bot_id、secret、admins、routes")
        for key in ("bot_id", "secret"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError(f"{key} 必须为非空字符串")
        admins = identifiers(value["admins"], "admins")
        raw_routes = value["routes"]
        if not isinstance(raw_routes, dict) or any(
            not isinstance(k, str) or not k.strip() or k != k.strip() for k in raw_routes
        ):
            raise ValueError("routes 必须为账号名到用户列表的对象")
        routes = {key: identifiers(users, "routes 的用户列表") for key, users in raw_routes.items()}
        if ADMIN_CHANNEL in routes:
            raise ValueError("admin 是管理员告警频道，收件人由 admins 配置，不能放入 routes")
        return cls(value["bot_id"], value["secret"], admins, routes)

    @property
    def allowed_users(self) -> frozenset[str]:
        return self.admins.union(*(users for users in self.routes.values()))

    def profiles(self, user: str) -> frozenset[str]:
        return frozenset(
            profile
            for profile, users in self.routes.items()
            if profile != ADMIN_CHANNEL and user in users
        )


EXAMPLE = """{
  "bot_id": "填写机器人 BotID",
  "secret": "填写机器人 Secret",
  "admins": ["admin-user-id"],
  "routes": {
    "nemo": ["admin-user-id", "user-id"],
    "wyf": ["admin-user-id"]
  }
}
"""
