"""Daily scheduling policy; observations and execution remain daemon-owned."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from maa_planner.daemon.execution.context import RunRequest
from maa_planner.daemon.planning.schedule import DailySchedule, ScheduledOccurrence

if TYPE_CHECKING:
    from maa_planner.daemon.planning.activity import ActivitySnapshot
    from maa_planner.daemon.tools.game_update import GameUpdateReport


@dataclass(frozen=True)
class ConfigSnapshot:
    revision: str
    content: bytes


@dataclass(frozen=True)
class Context:
    configuration: ConfigSnapshot
    running: tuple[str, ...]
    # All times are supplied by the caller; the policy does no I/O.
    now: float
    game_updates: GameUpdateReport | None = None
    game_update_error: str | None = None
    activity: ActivitySnapshot | None = None
    schedules: tuple[DailySchedule, ...] = ()
    enabled_profiles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    requests: tuple[RunRequest, ...] = ()
    wake_at: float | None = None
    occurrences: tuple[ScheduledOccurrence, ...] = ()


class Policy(Protocol):
    def evaluate(self, context: Context) -> Decision: ...
    def admit(self, request: RunRequest, context: Context) -> bool: ...


class EmptyPolicy:
    def evaluate(self, context: Context) -> Decision:
        return Decision()

    def admit(self, request: RunRequest, context: Context) -> bool:
        return False


class DailyPolicy:
    """Keep only the next occurrence per schedule/account, without doing I/O."""

    def __init__(self):
        self._revision: str | None = None
        self._next: dict[tuple[str, str], float] = {}

    def evaluate(self, context: Context) -> Decision:
        schedules = {s.id: s for s in context.schedules if s.enabled}
        if self._revision != context.configuration.revision:
            self._revision = context.configuration.revision
            self._next = {
                (schedule.id, profile): schedule.next_after(context.now, inclusive=True)
                for schedule in schedules.values()
                for profile in schedule.profiles
                if profile in context.enabled_profiles
            }
        due = []
        for key, timestamp in list(self._next.items()):
            schedule = schedules[key[0]]
            if timestamp <= context.now:
                following = schedule.next_after(timestamp)
                # A delayed wake uses the latest live window, never a backlog of stale runs.
                while following <= context.now:
                    timestamp, following = following, schedule.next_after(following)
                due.append(ScheduledOccurrence(key[0], key[1], timestamp, following))
                self._next[key] = following
        return Decision(wake_at=min(self._next.values(), default=None), occurrences=tuple(due))

    def admit(self, request: RunRequest, context: Context) -> bool:
        return request.config_revision == context.configuration.revision
