"""Explicit limits for offline asset acquisition; no game or daemon dependencies."""

import re
from pathlib import PurePosixPath


def validate_assets(arguments):
    fields = {
        "activity_id",
        "resolve_dependencies",
        "bundles",
        "max_bundle_bytes",
        "max_output_bytes",
        "export_timeout",
    }
    if not isinstance(arguments, dict) or set(arguments) != fields:
        raise ValueError(
            "assets 需要 activity_id、bundles、resolve_dependencies 和显式大小／超时预算"
        )
    if not isinstance(arguments["activity_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,80}", arguments["activity_id"]
    ):
        raise ValueError("活动素材身份无效")
    if type(arguments["resolve_dependencies"]) is not bool:
        raise ValueError("resolve_dependencies 必须显式为布尔值")
    bundles = arguments["bundles"]
    if (
        not isinstance(bundles, list)
        or not 1 <= len(bundles) <= 100
        or len(set(str(v).casefold() for v in bundles)) != len(bundles)
    ):
        raise ValueError("bundles 必须为 1..100 个不重复的 bundle 名称（包含显式依赖）")
    for name in bundles:
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_\[\]#./-]{1,200}\.ab", name)
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
        ):
            raise ValueError("bundle 名称无效或包含目录逃逸")
    for field, maximum in [
        ("max_bundle_bytes", 1024**3),
        ("max_output_bytes", 1024**3),
        ("export_timeout", 3600),
    ]:
        if type(arguments[field]) is not int or not 1 <= arguments[field] <= maximum:
            raise ValueError(f"{field} 必须为 1..{maximum} 的整数")
    return arguments
