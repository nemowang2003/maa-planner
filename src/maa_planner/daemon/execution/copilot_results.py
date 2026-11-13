"""Per-entry battle evidence, independent of whether the task chain exited cleanly."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True, slots=True)
class CopilotResult:
    task_id: int
    entry_id: int
    stage: str
    code: str
    is_raid: bool
    outcome: str = "unknown"
    reason: str = "not_started"
    observed_at: float | None = None
    evidence: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> CopilotResult:
        data: dict = dict(value)
        data["evidence"] = tuple(value.get("evidence", ()))
        return cls(**data)


class CopilotResults:
    def __init__(self):
        self.entries: dict[tuple[int, int], CopilotResult] = {}
        self.configs: dict[tuple[int, int], dict] = {}
        self.active: dict[int, tuple[int, int]] = {}

    def register(self, task_id: int, params: dict) -> None:
        for config in params.get("copilot_list", ()):
            identifier = config.get("id")
            if type(identifier) is not int:
                continue
            key = task_id, identifier
            if key in self.configs:
                raise ValueError("Copilot 列表项 id 重复，不能关联战果")
            self.configs[key] = config
            self.entries[key] = CopilotResult(
                task_id,
                identifier,
                "",
                config.get("nav_name_override", ""),
                bool(config.get("is_raid", False)),
            )

    def consume(self, message: int, payload: dict, reference: str, observed_at: float) -> None:
        if payload.get("taskchain") != "Copilot":
            return
        task_id = payload.get("taskid")
        if type(task_id) is not int:
            return
        details = payload.get("details", {})
        if not isinstance(details, dict):
            return
        if message == 20003 and payload.get("what") == "CopilotListLoadTaskFileSuccess":
            identifier = details.get("id")
            if type(identifier) is not int:
                return
            key = task_id, identifier
            config = self.configs.get(key)
            stage = details.get("stage_name")
            if (
                config is None
                or details.get("file_name") != config.get("filename")
                or not isinstance(stage, str)
                or not stage
            ):
                self.active.pop(task_id, None)
                return
            self.entries[key] = replace(
                self.entries[key],
                stage=stage,
                reason="no_settlement_evidence",
                observed_at=observed_at,
                evidence=(reference,),
            )
            self.active[task_id] = key
            return
        key = self.active.get(task_id)
        if key is None or payload.get("subtask") != "ProcessTask":
            return
        first = payload.get("first", [])
        if not isinstance(first, list) or not any(
            isinstance(name, str) and name.endswith("@WaitUntilEndOfAction") for name in first
        ):
            return
        task = details.get("task")
        task = task.rsplit("@", 1)[-1] if isinstance(task, str) else ""
        outcome = reason = None
        if message == 20002 and task in {"StageDrops-Stars-3", "StageDrops-Stars-Adverse"}:
            outcome, reason = "completed", "settlement_stars"
        elif message in {20001, 20002} and task == "FightMissionFailed":
            outcome, reason = "failed", "battle_failed"
        if outcome is not None:
            previous = self.entries[key]
            # A generic error after a recognized settlement does not erase it.
            if previous.outcome == "completed":
                return
            self.entries[key] = replace(
                previous,
                outcome=outcome,
                reason=reason,
                observed_at=observed_at,
                evidence=(*previous.evidence, reference),
            )

    def to_list(self) -> list[dict]:
        return [asdict(value) for value in self.entries.values()]
