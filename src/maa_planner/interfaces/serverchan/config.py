"""ServerChan3 route URLs are credentials, kept in an independent JSON file."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from platformdirs import user_config_path

APP_NAME = "maa-planner-serverchan-client"


def default_config_file() -> Path:
    return user_config_path(APP_NAME, appauthor=False, roaming=True) / "serverchan.json"


def validate_url(value) -> None:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError("路由 URL 必须为完整的 Server酱³ HTTPS 地址")
    try:
        parts = urlsplit(value)
        host = re.fullmatch(r"([0-9]+)\.push\.ft07\.com", parts.netloc)
        key = re.fullmatch(r"/send/sctp([0-9]+)t[A-Za-z0-9_-]+(?:\.send)?", parts.path)
        if (
            parts.scheme != "https"
            or host is None
            or key is None
            or host[1] != key[1]
            or parts.query
            or parts.fragment
        ):
            raise ValueError()
    except ValueError:
        # The full URL contains the SendKey; do not echo invalid input.
        raise ValueError(
            "需要 UID 匹配的 https://UID.push.ft07.com/send/sctpUIDt密钥.send 地址"
        ) from None


@dataclass(frozen=True)
class ServerChanConfig:
    routes: dict[str, tuple[str, ...]] = field(repr=False)

    @classmethod
    def load(cls, path: Path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError("Server酱配置不是有效 JSON") from None
        if not isinstance(value, dict):
            raise ValueError("配置必须为账号名到 URL 列表的映射")
        routes = {}
        for profile, urls in value.items():
            if not profile or profile != profile.strip() or not isinstance(urls, list):
                raise ValueError("配置必须为非空账号名到 URL 列表的映射")
            for url in urls:
                validate_url(url)
            if len(set(urls)) != len(urls):
                raise ValueError("同一账号的 URL 列表不能有重复项")
            routes[profile] = tuple(urls)
        return cls(routes)


EXAMPLE = """{
  "nemo": ["https://123.push.ft07.com/send/sctp123tREPLACE_ME.send"],
  "wyf": ["https://456.push.ft07.com/send/sctp456tREPLACE_ME.send"]
}
"""
