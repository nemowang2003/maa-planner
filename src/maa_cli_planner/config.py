from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs
import tomlkit

APP_NAME = "maa_cli_planner"


def default_config_file() -> Path:
    return Path.home() / ".config" / APP_NAME / "config.toml"


def default_skland_auth_dir() -> Path:
    return platformdirs.user_config_path("skland-api") / "auth"


def default_maa_config_dir() -> Path:
    return platformdirs.user_config_path("maa")


def _expand_path(raw: str | None, default: Path) -> Path:
    if raw is None:
        return default
    return Path(raw).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class PlannerUser:
    name: str
    auth_name: str
    character_name: str | None = None
    enabled: bool = True
    material_weights: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannerSettings:
    config_file: Path
    maa_config_dir: Path
    skland_auth_dir: Path
    task_prefix: str
    profile_name: str
    write_profile: bool
    client_type: str
    fallback_stage: str
    fight_times: int
    expiring_medicine: int
    expiring_stone: int
    recruit_refresh: bool
    recruit_select: list[int]
    recruit_confirm: list[int]
    recruit_times: int
    infrast_filename: str
    infrast_facility: list[str]
    dorm_trust_enabled: bool
    drone_threshold: int
    material_weights: dict[str, int]
    users: list[PlannerUser]
    profile_sections: dict[str, dict[str, Any]]


def load_settings(config_file: Path | None = None) -> PlannerSettings:
    path = (config_file or default_config_file()).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}，请先运行 `maa_cli_planner init`")
    with path.open("rb") as fp:
        raw = tomllib.load(fp)

    planner = raw.get("planner", {})
    defaults = raw.get("defaults", {})
    recruit = raw.get("recruit", {})
    infrast = raw.get("infrast", {})
    global_materials = _normalize_int_map(raw.get("materials", {}), "materials")
    users = _load_users(raw.get("users", {}), global_materials)
    profile_sections = _normalize_profile(raw.get("profile", {}))

    return PlannerSettings(
        config_file=path,
        maa_config_dir=_expand_path(planner.get("maa_config_dir"), default_maa_config_dir()),
        skland_auth_dir=_expand_path(planner.get("skland_auth_dir"), default_skland_auth_dir()),
        task_prefix=str(planner.get("task_prefix", "daily")),
        profile_name=str(planner.get("profile_name", "default")),
        write_profile=bool(planner.get("write_profile", False)),
        client_type=str(defaults.get("client_type", "Official")),
        fallback_stage=str(defaults.get("fallback_stage", "1-7")),
        fight_times=int(defaults.get("fight_times", 99)),
        expiring_medicine=int(defaults.get("expiring_medicine", 0)),
        expiring_stone=int(defaults.get("expiring_stone", 0)),
        recruit_refresh=bool(recruit.get("refresh", True)),
        recruit_select=_normalize_int_list(recruit.get("select", [4]), "recruit.select"),
        recruit_confirm=_normalize_int_list(recruit.get("confirm", [3]), "recruit.confirm"),
        recruit_times=int(recruit.get("times", 4)),
        infrast_filename=str(infrast.get("filename", "default.json")),
        infrast_facility=_normalize_str_list(
            infrast.get(
                "facility",
                ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm"],
            ),
            "infrast.facility",
        ),
        dorm_trust_enabled=bool(infrast.get("dorm_trust_enabled", True)),
        drone_threshold=int(infrast.get("drone_threshold", 180)),
        material_weights=global_materials,
        users=users,
        profile_sections=profile_sections,
    )


def _load_users(raw_users: Any, global_materials: dict[str, int]) -> list[PlannerUser]:
    if not isinstance(raw_users, dict) or not raw_users:
        raise ValueError("配置文件缺少 [users] 段")

    users: list[PlannerUser] = []
    for name, raw in raw_users.items():
        if not isinstance(raw, dict):
            raise ValueError(f"[users.{name}] 必须是 table")
        user_materials = global_materials | _normalize_int_map(
            raw.get("materials", {}), f"users.{name}.materials"
        )
        users.append(
            PlannerUser(
                name=name,
                auth_name=str(raw.get("auth_name", name)),
                character_name=(
                    None if raw.get("character_name") is None else str(raw.get("character_name"))
                ),
                enabled=bool(raw.get("enabled", True)),
                material_weights=user_materials,
            )
        )
    return users


def _normalize_profile(raw_profile: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_profile, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for section, payload in raw_profile.items():
        if isinstance(payload, dict):
            result[section] = payload
    return result


def _normalize_int_map(raw: Any, name: str) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{name} 必须是 table")
    return {str(k): int(v) for k, v in raw.items()}


def _normalize_int_list(raw: Any, name: str) -> list[int]:
    if not isinstance(raw, list):
        raise ValueError(f"{name} 必须是整数列表")
    return [int(v) for v in raw]


def _normalize_str_list(raw: Any, name: str) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{name} 必须是字符串列表")
    return [str(v) for v in raw]


def build_example_config() -> str:
    doc = tomlkit.document()

    planner = tomlkit.table()
    planner["maa_config_dir"] = str(default_maa_config_dir())
    planner["skland_auth_dir"] = str(default_skland_auth_dir())
    planner["task_prefix"] = "daily"
    planner["profile_name"] = "default"
    planner["write_profile"] = False
    doc["planner"] = planner

    defaults = tomlkit.table()
    defaults["client_type"] = "Official"
    defaults["fallback_stage"] = "1-7"
    defaults["fight_times"] = 99
    defaults["expiring_medicine"] = 0
    defaults["expiring_stone"] = 0
    doc["defaults"] = defaults

    recruit = tomlkit.table()
    recruit["refresh"] = True
    recruit["select"] = [4]
    recruit["confirm"] = [3]
    recruit["times"] = 4
    doc["recruit"] = recruit

    infrast = tomlkit.table()
    infrast["filename"] = "default.json"
    infrast["facility"] = ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm"]
    infrast["dorm_trust_enabled"] = True
    infrast["drone_threshold"] = 180
    doc["infrast"] = infrast

    materials = tomlkit.table()
    materials["固源岩组"] = 8
    materials["糖组"] = 6
    materials["酮凝集组"] = 5
    doc["materials"] = materials

    users = tomlkit.table()
    main_user = tomlkit.table()
    main_user["auth_name"] = "main"
    users["main"] = main_user
    alt_user = tomlkit.table()
    alt_user["auth_name"] = "alt"
    alt_user["enabled"] = False
    users["alt"] = alt_user
    doc["users"] = users

    profile = tomlkit.table()
    connection = tomlkit.table()
    connection["preset"] = "MuMuPro"
    connection["adb_path"] = "/Applications/MAA.app/Contents/MacOS/adb"
    connection["address"] = "127.0.0.1:16384"
    profile["connection"] = connection
    startup = tomlkit.table()
    startup["client_type"] = "Official"
    profile["startup"] = startup
    doc["profile"] = profile

    return doc.as_string()
