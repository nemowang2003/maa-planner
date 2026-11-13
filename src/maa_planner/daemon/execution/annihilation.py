"""Business facts from annihilation callbacks, independent of chain exit codes."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AnnihilationResult:
    task_id: int
    stage: str
    outcome: str = "unknown"
    reason: str = "missing_evidence"
    current: int | None = None
    total: int | None = None
    observed_at: float | None = None
    evidence: tuple[str, ...] = ()
    rescue: dict | None = None

    @classmethod
    def from_dict(cls, value: dict):
        data: dict = dict(value)
        data["evidence"] = tuple(value.get("evidence", ()))
        return cls(**data)
