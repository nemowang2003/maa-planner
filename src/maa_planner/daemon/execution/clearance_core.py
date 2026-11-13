"""Run project navigation and one Copilot battle at a time on an owned Core."""

from __future__ import annotations

import time

from maa_planner.daemon.execution.activity_copilot import TargetObservation

NAVIGATION_ATTEMPTS = 3
NAVIGATION_RETRY_DELAY = 1.0


def task_error(identifier, result):
    if result.get("status") in {"error", "failed"}:
        return {
            "task_id": identifier,
            "status": result["status"],
            "evidence": result.get("error_evidence"),
        }
    return None


def navigation_checkpoint(prefix: str, phase: str, nodes: list) -> str:
    """Resume the next non-consuming recognition step in our own graph."""
    names = {name for name, _ in nodes}
    if phase == "Terminal" and names & {"TerminalDefault", "Terminal"}:
        return prefix + "@TerminalReady"
    return prefix + "@" + phase


class CoreClearanceDriver:
    def __init__(self, plan: dict, execute_tasks, results, stop):
        self.configs = {(e["stage_id"], e["is_raid"]): e for e in plan["targets"]}
        self.available_from, self.available_until = plan["available_from"], plan["available_until"]
        self.execute_tasks, self.results, self.stop = execute_tasks, results, stop
        self.zone_id = None
        self.interface_evidence: tuple[str, ...] = ()
        self.formation_group: str | None = None
        self.pending_settlement: tuple[str, bool] | None = None
        self.completed = {tuple(v) for v in plan["completed"]}

    def cancelled(self):
        return self.stop.is_set()

    def navigate(self, target):
        if not self.available_from <= time.time() < self.available_until:
            return TargetObservation("activity_closed", ("activity_snapshot",))
        config = self.configs[target.stage_id, target.is_raid]
        if config.get("kind") == "missing_tutorial":
            return TargetObservation("no_tutorial_copilot")
        if config.get("kind") == "tail_story" and any(
            tuple(stage) not in self.completed for stage in config["prerequisites"]
        ):
            return TargetObservation("prerequisites_unconfirmed")
        prefix = config["navigation"]
        if self.pending_settlement is not None:
            return self.exit_settlement(target)
        if self.zone_id == config["zone_id"]:
            return TargetObservation("ready", self.interface_evidence)
        evidence = []
        phases = (
            ("SwitchArea",)
            if self.zone_id is not None
            else ("ToHome", "Terminal", "Activity", "Area")
        )
        self.zone_id = None
        for phase in phases:
            entry = prefix + "@" + phase
            attempts = 1 if phase == "SwitchArea" else NAVIGATION_ATTEMPTS
            for attempt in range(attempts):
                if self.cancelled():
                    return TargetObservation("cancelled")
                if not self.available_from <= time.time() < self.available_until:
                    return TargetObservation("activity_closed", ("activity_snapshot",))
                ids = self.execute_tasks(
                    [
                        {
                            "type": "Custom",
                            "name": f"{target.code}:{phase}",
                            "params": {"task_names": [entry]},
                        }
                    ],
                    handled=True,
                )
                if self.cancelled():
                    return TargetObservation("cancelled")
                if not ids:
                    return TargetObservation("navigation_failed", tuple(evidence))
                nodes = self.results.nodes.get(ids[0], [])
                if any(name == "Locked" for name, _ in nodes):
                    return TargetObservation(
                        "locked", tuple(ref for name, ref in nodes if name == "Locked")
                    )
                status = self.results.tasks[ids[0]]["status"]
                if status == "completed":
                    if phase in {"Area", "SwitchArea"} and not any(
                        name == "Map" for name, _ in nodes
                    ):
                        if phase == "SwitchArea":
                            return self.navigate(target)
                        return TargetObservation("navigation_failed", tuple(evidence))
                    break
                if status not in {"error", "failed"} or attempt + 1 == attempts:
                    if phase == "SwitchArea" and status in {"error", "failed"}:
                        return self.navigate(target)
                    return TargetObservation(
                        "navigation_failed",
                        tuple(evidence),
                        task_error(ids[0], self.results.tasks[ids[0]]),
                    )
                if nodes:
                    entry = navigation_checkpoint(prefix, phase, nodes)
                if self.stop.wait(NAVIGATION_RETRY_DELAY):
                    return TargetObservation("cancelled")
            if phase in {"Area", "SwitchArea"}:
                evidence.extend(ref for name, ref in nodes if name == "Map")
        self.zone_id = config["zone_id"]
        self.interface_evidence = tuple(evidence)
        return TargetObservation("ready", self.interface_evidence)

    def battle(self, target):
        if not self.available_from <= time.time() < self.available_until:
            return TargetObservation("activity_closed", ("activity_snapshot",))
        config = self.configs[target.stage_id, target.is_raid]
        if config.get("kind") == "tail_story":
            return self.watch_story(target, config)
        formation_group = config.get("formation_group")
        with_formation = formation_group is None or formation_group != self.formation_group
        self.formation_group = None
        ids = self.execute_tasks(
            [
                {
                    "type": "Copilot",
                    "name": target.code,
                    "params": {
                        "copilot_list": [
                            {
                                "id": 0,
                                "filename": config["filename"],
                                "nav_name_override": target.code,
                                "is_raid": target.is_raid,
                            }
                        ],
                        "formation": with_formation,
                        "ignore_requirements": False,
                        "use_sanity_potion": True,
                        "support_unit_usage": config.get("support_unit_usage", 0),
                    },
                }
            ],
            handled=True,
        )
        if not ids:
            return TargetObservation("unknown")
        result = self.results.copilot.entries.get((ids[0], 0))
        if result is not None and result.stage == config["script_stage_id"]:
            if result.outcome in {"completed", "failed"}:
                if result.outcome == "failed":
                    self.pending_settlement = (target.stage_id, target.is_raid)
                else:
                    self.formation_group = formation_group
                    self.completed.add((target.stage_id, target.is_raid))
                return TargetObservation(result.outcome, result.evidence)
        if self.cancelled():
            return TargetObservation("cancelled")
        # A two-star settlement is an explicit failed clearance. Do not treat a
        # generic chain error (navigation, disconnection, medicine, etc.) as loss.
        probes = self.execute_tasks(
            [
                {
                    "type": "Custom",
                    "name": "clearance-settlement",
                    "params": {
                        "task_names": [config["navigation"] + "@NonPerfect"],
                    },
                }
            ],
            handled=True,
        )
        if probes:
            proof = tuple(
                ref for name, ref in self.results.nodes.get(probes[0], []) if name == "NonPerfect"
            )
            if proof:
                self.pending_settlement = (target.stage_id, target.is_raid)
                return TargetObservation("failed", proof)
        self.zone_id = None
        return TargetObservation(
            "unknown", core_error=task_error(ids[0], self.results.tasks[ids[0]])
        )

    def watch_story(self, target, config):
        if self.cancelled():
            return TargetObservation("cancelled")
        if any(tuple(stage) not in self.completed for stage in config["prerequisites"]):
            return TargetObservation("prerequisites_unconfirmed")
        self.formation_group = None
        ids = self.execute_tasks(
            [
                {
                    "type": "Custom",
                    "name": target.code + ":story",
                    "params": {"task_names": [config["navigation"] + "@PlotBegin"]},
                }
            ],
            handled=True,
        )
        self.zone_id = None
        if self.cancelled():
            return TargetObservation("cancelled")
        if not ids:
            return TargetObservation("story_unverified")
        identifier = ids[0]
        steps = iter(("PlotPanel", "PlotStart", "PlotSkipped", "PlotReturned"))
        expected = next(steps)
        evidence = []
        for name, reference in self.results.nodes.get(identifier, []):
            if name == expected:
                evidence.append(reference)
                expected = next(steps, None)
                if expected is None:
                    break
        result = self.results.tasks[identifier]
        if expected is None and result["status"] == "completed":
            self.completed.add((target.stage_id, False))
            return TargetObservation("completed", tuple(evidence))
        return TargetObservation(
            "story_unverified", tuple(evidence), task_error(identifier, result)
        )

    def exit_settlement(self, target):
        if self.pending_settlement != (target.stage_id, target.is_raid):
            return TargetObservation("settlement_exit_failed")
        # The previous Copilot has finished. Reuse its own exit flow; the next
        # Copilot verifies/selects this same stage again, without a home detour.
        ids = self.execute_tasks(
            [
                {
                    "type": "Custom",
                    "name": target.code + ":settlement-exit",
                    "params": {"task_names": ["Copilot@ClickCornerUntilStartButton"]},
                }
            ],
            handled=True,
        )
        if self.cancelled():
            return TargetObservation("cancelled")
        if not ids:
            return TargetObservation("settlement_exit_failed")
        identifier = ids[0]
        proof = tuple(
            ref
            for name, ref in self.results.nodes.get(identifier, ())
            if name in {"BattleStartPreFlag", "BattleStartPreFlag-Return"}
        )
        result = self.results.tasks[identifier]
        if result["status"] != "completed" or not proof:
            self.zone_id = None
            return TargetObservation(
                "settlement_exit_failed", proof, task_error(identifier, result)
            )
        self.pending_settlement = None
        self.interface_evidence = proof
        return TargetObservation("ready", proof)
