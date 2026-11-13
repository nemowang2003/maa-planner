"""One automatic clearance phase; retries never advance past a failed target."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol

from maa_planner.daemon.planning.copilot import CopilotTarget


@dataclass(frozen=True, slots=True)
class TargetObservation:
    outcome: str
    evidence: tuple[str, ...] = ()
    core_error: dict | None = None


@dataclass(frozen=True, slots=True)
class ClearanceResult:
    target: CopilotTarget
    outcome: str
    failures: int
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClearanceReport:
    outcome: str
    results: tuple[ClearanceResult, ...]
    warning: dict | None = None
    farming_gate: dict | None = None
    selection: tuple[dict, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> ClearanceReport:
        return cls(
            value["outcome"],
            tuple(
                ClearanceResult(
                    CopilotTarget(**r["target"]), r["outcome"], r["failures"], tuple(r["evidence"])
                )
                for r in value["results"]
            ),
            value.get("warning"),
            value.get("farming_gate"),
            tuple(value.get("selection", ())),
        )


def farming_gate(plan: dict, completed: set | frozenset) -> dict:
    required = plan.get("required", [])
    missing = [v for v in required if tuple(v) not in completed]
    complete = plan.get("catalog_complete", False) and bool(required)
    return {
        "allowed": complete and not missing,
        "reason": "catalog_incomplete"
        if not complete
        else "stages_incomplete"
        if missing
        else "ready",
        "missing": missing,
    }


def validate_clearance_plan(plan: object, task_count: int) -> None:
    required_fields = {
        "activity_id",
        "activity_name",
        "targets",
        "completed",
        "catalog_commit",
        "before_task",
        "available_from",
        "available_until",
    }
    scope_fields = {"required", "catalog_complete"}
    if (
        not isinstance(plan, dict)
        or not required_fields <= plan.keys()
        or plan.keys() - required_fields - scope_fields - {"selection"}
        or (plan.keys() & scope_fields and not scope_fields <= plan.keys())
    ):
        raise ValueError("自动通关计划字段无效")
    if "selection" in plan and (
        not isinstance(plan["selection"], list)
        or any(
            not isinstance(v, dict) or v.get("status") not in {"selected", "no_usable_set"}
            for v in plan["selection"]
        )
    ):
        raise ValueError("自动通关作业集选择记录无效")
    if type(plan["before_task"]) is not int or not 0 <= plan["before_task"] < task_count:
        raise ValueError("自动通关阶段位置无效")
    if (
        any(
            type(plan[k]) not in (int, float) or not math.isfinite(plan[k])
            for k in ("available_from", "available_until")
        )
        or plan["available_until"] <= plan["available_from"]
    ):
        raise ValueError("自动通关开放时间无效")
    for key in ("activity_id", "activity_name", "catalog_commit"):
        if not isinstance(plan[key], str) or not plan[key]:
            raise ValueError("自动通关活动标识缺失")
    if not isinstance(plan["targets"], list) or len(plan["targets"]) > 200:
        raise ValueError("自动通关目标列表无效")
    seen = set()
    for entry in plan["targets"]:
        entry_fields = {
            "script_id",
            "stage_id",
            "code",
            "is_raid",
            "game_stage_id",
            "filename",
            "script_stage_id",
            "navigation",
            "zone_id",
        }
        barrier = isinstance(entry, dict) and entry.get("kind") == "missing_tutorial"
        story = isinstance(entry, dict) and entry.get("kind") == "tail_story"
        special = barrier or story
        if special:
            entry_fields.remove("filename")
        if story:
            entry_fields.add("prerequisites")
        if (
            not isinstance(entry, dict)
            or not entry_fields <= entry.keys()
            or entry.keys() - entry_fields - {"support_unit_usage", "formation_group", "kind"}
        ):
            raise ValueError("自动通关目标字段无效")
        if "kind" in entry and not special:
            raise ValueError("自动通关目标种类无效")
        if special and (
            entry["script_id"] != 0
            or entry["is_raid"] is not False
            or entry["stage_id"] != entry["game_stage_id"]
            or entry["stage_id"] != entry["script_stage_id"]
            or {"formation_group", "support_unit_usage"} & entry.keys()
        ):
            raise ValueError("非作战目标不能包含 Copilot 或突袭参数")
        if story and (
            not isinstance(entry["prerequisites"], list)
            or any(
                not isinstance(v, list)
                or len(v) != 2
                or not isinstance(v[0], str)
                or not v[0]
                or type(v[1]) is not bool
                for v in entry["prerequisites"]
            )
            or len({tuple(v) for v in entry["prerequisites"]}) != len(entry["prerequisites"])
            or [entry["stage_id"], False] in entry["prerequisites"]
        ):
            raise ValueError("拖尾剧情前置关卡无效")
        if type(entry.get("support_unit_usage", 0)) is not int or entry.get(
            "support_unit_usage", 0
        ) not in (0, 1):
            raise ValueError("自动通关助战参数无效")
        if "formation_group" in entry and (
            not isinstance(entry["formation_group"], str)
            or not re.fullmatch(r"set-[1-9][0-9]*", entry["formation_group"])
        ):
            raise ValueError("自动通关编队复用标识无效")
        if (
            type(entry["script_id"]) is not int
            or entry["script_id"] < (0 if special else 1)
            or type(entry["is_raid"]) is not bool
        ):
            raise ValueError("自动通关作业标识或难度无效")
        strings = ["stage_id", "code", "game_stage_id", "script_stage_id", "zone_id"]
        if not special:
            strings.append("filename")
        for key in strings:
            if not isinstance(entry[key], str) or not entry[key]:
                raise ValueError("自动通关关卡数据缺失")
        if not isinstance(entry["navigation"], str) or not re.fullmatch(
            r"Planner@[A-Za-z0-9_@-]+", entry["navigation"]
        ):
            raise ValueError("自动通关导航必须使用私有节点")
        key = entry["stage_id"], entry["is_raid"]
        if key in seen:
            raise ValueError("自动通关目标重复")
        seen.add(key)
    if not isinstance(plan["completed"], list) or any(
        not isinstance(v, list)
        or len(v) != 2
        or not isinstance(v[0], str)
        or type(v[1]) is not bool
        for v in plan["completed"]
    ):
        raise ValueError("自动通关完成快照无效")
    if "required" in plan:
        required = plan["required"]
        if (
            type(plan["catalog_complete"]) is not bool
            or not isinstance(required, list)
            or any(
                not isinstance(v, list)
                or len(v) != 2
                or not isinstance(v[0], str)
                or not v[0]
                or type(v[1]) is not bool
                for v in required
            )
            or len({tuple(v) for v in required}) != len(required)
            or (plan["catalog_complete"] and not required)
        ):
            raise ValueError("自动通关完整活动清单无效")


class ClearanceDriver(Protocol):
    """Each observation must be positive evidence from the current account/UI."""

    def cancelled(self) -> bool: ...

    def navigate(self, target: CopilotTarget) -> TargetObservation:
        """Navigate, confirm stage and mode, without inferring historical completion."""
        ...

    def battle(self, target: CopilotTarget) -> TargetObservation:
        """One battle with medicine allowed; return only verified outcome."""
        ...


def run_clearance(
    targets: tuple[CopilotTarget, ...],
    completed: frozenset[tuple[str, bool]],
    driver: ClearanceDriver,
    on_result=None,
) -> ClearanceReport:
    results: list[ClearanceResult] = []

    def halted(target: CopilotTarget, reason: str, failures: int, evidence=(), core_error=None):
        return ClearanceReport(
            "stopped",
            tuple(results),
            {
                "stage_id": target.stage_id,
                "is_raid": target.is_raid,
                "reason": reason,
                "failures": failures,
                "evidence": list(evidence),
                **({"core_error": core_error} if core_error else {}),
            },
        )

    for target in targets:
        if driver.cancelled():
            return ClearanceReport("cancelled", tuple(results))
        if (target.stage_id, target.is_raid) in completed:
            results.append(ClearanceResult(target, "already_completed", 0, ()))
            continue
        failures = 0
        evidence: tuple[str, ...] = ()
        while True:
            if driver.cancelled():
                return ClearanceReport("cancelled", tuple(results))
            try:
                observation = driver.navigate(target)
            except InterruptedError:
                return ClearanceReport("cancelled", tuple(results))
            if driver.cancelled() or observation.outcome == "cancelled":
                return ClearanceReport("cancelled", tuple(results))
            evidence += observation.evidence
            if observation.outcome != "ready" or not observation.evidence:
                return halted(
                    target,
                    observation.outcome
                    if observation.outcome
                    in {
                        "locked",
                        "navigation_failed",
                        "activity_closed",
                        "no_tutorial_copilot",
                        "settlement_exit_failed",
                        "prerequisites_unconfirmed",
                    }
                    else "unknown",
                    failures,
                    evidence,
                    observation.core_error,
                )
            if driver.cancelled():
                return ClearanceReport("cancelled", tuple(results))
            try:
                battle = driver.battle(target)
            except InterruptedError:
                return ClearanceReport("cancelled", tuple(results))
            evidence += battle.evidence
            if battle.outcome == "completed" and battle.evidence:
                item = ClearanceResult(target, "completed", failures, evidence)
                results.append(item)
                if on_result is not None:
                    on_result(item)
                if driver.cancelled():
                    return ClearanceReport("cancelled", tuple(results))
                break
            if driver.cancelled() or battle.outcome == "cancelled":
                return ClearanceReport("cancelled", tuple(results))
            if battle.outcome != "failed" or not battle.evidence:
                return halted(
                    target,
                    "story_unverified"
                    if battle.outcome == "story_unverified"
                    else "battle_unknown",
                    failures,
                    evidence,
                    battle.core_error,
                )
            failures += 1
            if failures == 3:
                results.append(ClearanceResult(target, "failed", failures, evidence))
                return halted(target, "three_consecutive_defeats", failures, evidence)
            # Re-enter and verify this same target before retrying. A successful
            # navigation does not reset the count; only battle success does.
    return ClearanceReport("completed", tuple(results))
