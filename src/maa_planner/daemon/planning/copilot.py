"""Script suitability and game-stage metadata are separate from player progress."""

from __future__ import annotations

from dataclasses import dataclass


def script_modes(script: dict) -> tuple[bool, ...]:
    """difficulty=0/missing is normal-only by the user's explicit policy."""
    difficulty = script.get("difficulty", 0)
    if type(difficulty) is not int or difficulty not in {0, 1, 2, 3}:
        raise ValueError("作业 difficulty 必须为 0、1、2、3")
    return {0: (False,), 1: (False,), 2: (True,), 3: (False, True)}[difficulty]


@dataclass(frozen=True, slots=True)
class StageDefinition:
    stage_id: str
    code: str
    zone_id: str
    difficulty: str
    raid_stage_id: str | None
    prerequisites: tuple[tuple[str, str], ...]


def stage_definitions(
    document: dict, stage_ids: tuple[str, ...] | None = None
) -> dict[str, StageDefinition]:
    """Read stage_table's explicit hardStagedId, never infer raids from EX names."""
    stages = document.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("关卡表缺少 stages")
    if stage_ids is not None:
        wanted = set(stage_ids)
        for identifier in stage_ids:
            value = stages.get(identifier)
            if not isinstance(value, dict):
                raise ValueError(f"关卡表没有作业对应关卡: {identifier}")
            if isinstance(value.get("hardStagedId"), str):
                wanted.add(value["hardStagedId"])
        stages = {key: value for key, value in stages.items() if key in wanted}
    result = {}
    for identifier, value in stages.items():
        if not isinstance(value, dict) or value.get("difficulty") not in {"NORMAL", "FOUR_STAR"}:
            continue
        if not all(isinstance(value.get(k), str) and value[k] for k in ("code", "zoneId")):
            raise ValueError("普通关卡缺少 code/zoneId")
        if value.get("stageId") != identifier or "hardStagedId" not in value:
            raise ValueError("关卡表标识或突袭关系缺失")
        raid = value["hardStagedId"]
        if raid is not None:
            target = stages.get(raid) if isinstance(raid, str) else None
            if (
                not isinstance(target, dict)
                or target.get("stageId") != raid
                or target.get("difficulty") != "FOUR_STAR"
                or target.get("code") != value["code"]
                or target.get("zoneId") != value["zoneId"]
            ):
                raise ValueError(f"关卡 {identifier} 的突袭关系矛盾或缺失")
        conditions = value.get("unlockCondition")
        if not isinstance(conditions, list) or any(
            not isinstance(c, dict)
            or not isinstance(c.get("stageId"), str)
            or c.get("completeState") not in {"PASS", "COMPLETE"}
            for c in conditions
        ):
            raise ValueError(f"关卡 {identifier} 的前置条件无效")
        result[identifier] = StageDefinition(
            identifier,
            value["code"],
            value["zoneId"],
            value["difficulty"],
            raid,
            tuple((c["stageId"], c["completeState"]) for c in conditions),
        )
    return result


@dataclass(frozen=True, slots=True)
class CopilotTarget:
    script_id: int
    stage_id: str
    code: str
    is_raid: bool
    game_stage_id: str = ""


def targets(
    scripts: tuple[tuple[int, dict], ...], definitions: dict[str, StageDefinition]
) -> tuple[CopilotTarget, ...]:
    """Expand compatible modes; use one script per target without duplicate battles."""
    selected: dict[tuple[str, bool], CopilotTarget] = {}
    for identifier, script in scripts:
        stage = script.get("stage_name")
        definition = definitions.get(stage) if isinstance(stage, str) else None
        if definition is None or definition.difficulty != "NORMAL":
            raise ValueError(f"关卡表没有作业对应的普通关卡: {stage}")
        for raid in script_modes(script):
            if raid and definition.raid_stage_id is None:
                continue
            target_id = definition.raid_stage_id if raid else definition.stage_id
            assert target_id is not None
            selected.setdefault(
                (target_id, raid),
                CopilotTarget(identifier, definition.stage_id, definition.code, raid, target_id),
            )
    # Sort stage groups by prerequisites, then ordinary -> raid within each
    # group. This deliberately does not batch all ordinary stages first.
    groups: dict[str, list[CopilotTarget]] = {}
    for target in selected.values():
        groups.setdefault(target.stage_id, []).append(target)
    output = []
    while groups:
        pending_ids = {
            target.game_stage_id: stage for stage, entries in groups.items() for target in entries
        }
        for stage, entries in list(groups.items()):
            dependencies = {
                identifier
                for target in entries
                for identifier, _ in definitions[target.game_stage_id].prerequisites
            }
            if any(
                identifier in pending_ids and pending_ids[identifier] != stage
                for identifier in dependencies
            ):
                continue
            output.extend(sorted(entries, key=lambda target: target.is_raid))
            del groups[stage]
            break
        else:
            raise ValueError("作业目标的解锁依赖存在环")
    return tuple(output)
