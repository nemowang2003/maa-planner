"""Structured activity-shop outcome, separate from process/task-chain status."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EventShopResult:
    task_id: int | None = None
    outcome: str = "unknown"
    reason: str = "missing_evidence"
    evidence: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> EventShopResult:
        data: dict = dict(value)
        data["evidence"] = tuple(value.get("evidence", ()))
        return cls(**data)
