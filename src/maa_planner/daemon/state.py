"""Small, single-writer SQLite store for infrastructure state only."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

from maa_planner.daemon.execution.context import RunRequest, RunResult
from maa_planner.daemon.history import HistoryLog
from maa_planner.schema import TERMINAL, APIError

FINAL = TERMINAL - {"recovery_required"}


class StateStore:
    def __init__(self, path: Path, *, on_event: Callable[[str, dict], None] | None = None):
        self.on_event = None
        self.history = HistoryLog(path.parent / "logs" / (path.stem + "-history.jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute("PRAGMA foreign_keys = ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14):
            self.db.close()
            raise ValueError(f"不支持的状态数据库版本: {version}")
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS configurations (revision TEXT PRIMARY KEY, content BLOB NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS game_resource_pending (profile TEXT PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS maintenance_login (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
            )
            self.db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, revision TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS copilot_requests (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE,
                error TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, error TEXT, cancel_requested INTEGER NOT NULL)""")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS prepared_executions (attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), payload TEXT NOT NULL)"
            )
            self.db.execute("""CREATE TABLE IF NOT EXISTS scheduled_runs (
                id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, profile TEXT NOT NULL,
                scheduled_at REAL NOT NULL, expires_at REAL NOT NULL, revision TEXT NOT NULL,
                status TEXT NOT NULL, attempt_id TEXT, error TEXT)""")
            for identifier, payload in self.db.execute(
                "SELECT id, payload FROM copilot_requests WHERE id NOT IN (SELECT id FROM operations)"
            ).fetchall():
                command = json.loads(payload)
                if "profile" not in command or "url" not in command:
                    continue
                operation = {
                    "action": "copilot",
                    "target": command["profile"],
                    "expires_at": None,
                    "arguments": {
                        key: value
                        for key, value in command.items()
                        if key not in {"request_id", "profile"}
                    },
                }
                self.db.execute(
                    "INSERT INTO operations VALUES (?, ?, 'preparing', NULL, NULL, 0)",
                    (identifier, json.dumps(operation, ensure_ascii=False)),
                )
            self.db.execute("""CREATE TABLE IF NOT EXISTS alerts (
                profile TEXT NOT NULL, category TEXT NOT NULL, subject TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(profile, category, subject))""")
            # Weekly progress comes from Skland on every batch; attempts and
            # Core observations must never suppress a later batch.
            self.db.execute("DROP TABLE IF EXISTS weekly_annihilation")
            self.db.execute("""CREATE TABLE IF NOT EXISTS stage_completions (
                profile TEXT NOT NULL, activity_id TEXT NOT NULL, stage_id TEXT NOT NULL,
                is_raid INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(profile, activity_id, stage_id, is_raid))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS activity_shops (
                profile TEXT NOT NULL, activity_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(profile, activity_id, fingerprint))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS activity_shop_navigation (
                activity_id TEXT NOT NULL, starts_at REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(activity_id, starts_at))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS activity_windows (
                name TEXT NOT NULL, starts_at REAL NOT NULL, ends_at REAL NOT NULL,
                activity_key TEXT NOT NULL, checked_at REAL NOT NULL, ended_at REAL,
                PRIMARY KEY(name, starts_at))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS orundum_observation_wait (
                profile TEXT PRIMARY KEY, applied_at REAL NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS orundum_intents (
                profile TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS scheduled_retries (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS schedule_cursors (schedule_id TEXT NOT NULL, profile TEXT NOT NULL, scheduled_at REAL NOT NULL, PRIMARY KEY(schedule_id,profile))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS configuration_head (revision TEXT PRIMARY KEY)"
            )
            if version < 14:
                self._migrate_history()
            self.db.execute("DROP TABLE IF EXISTS events")
            self.db.execute("PRAGMA user_version = 14")
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS daily_controls (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
        self.on_event = on_event

    def _migrate_history(self) -> None:
        head = self.db.execute(
            "SELECT revision FROM configurations ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if head:
            self.db.execute("INSERT OR IGNORE INTO configuration_head VALUES (?)", head)
        for sequence, kind, payload in self.db.execute(
            "SELECT sequence,kind,payload FROM events ORDER BY sequence"
        ):
            self.history.event(kind, json.loads(payload), sequence=sequence)
        for identifier, result in self.db.execute(
            "SELECT id,result FROM attempts WHERE result IS NOT NULL"
        ).fetchall():
            value = RunResult.from_dict(json.loads(result))
            if value.status in FINAL:
                self.finish(value)
        for identifier, status in self.db.execute("SELECT id,status FROM operations").fetchall():
            if status in FINAL:
                self._archive_operation(identifier)
        for (identifier,) in self.db.execute(
            "SELECT id FROM copilot_requests WHERE error IS NOT NULL"
        ).fetchall():
            self._archive_copilot(identifier)
        for identifier, status, schedule, profile, at in self.db.execute(
            "SELECT id,status,schedule_id,profile,scheduled_at FROM scheduled_runs"
        ).fetchall():
            self.db.execute(
                "INSERT INTO schedule_cursors VALUES (?,?,?) ON CONFLICT(schedule_id,profile) DO UPDATE SET scheduled_at=max(scheduled_at,excluded.scheduled_at)",
                (schedule, profile, at),
            )
            if status in FINAL | {"skipped", "expired"}:
                self._archive_schedule(identifier)
        for retry in self.scheduled_retries():
            if retry["status"] not in {"waiting", "running", "recovery_required"}:
                self.save_scheduled_retry(retry)
        for profile, category, subject, payload in self.db.execute(
            "SELECT * FROM alerts"
        ).fetchall():
            value = json.loads(payload)
            details = value.get("details", {})
            if category == "task_retry" and details.get("status") not in {
                "waiting",
                "running",
                "recovery_required",
            }:
                self.history.save("alert", str(value["alert_id"]), value)
                details = {
                    k: v
                    for k, v in details.items()
                    if k not in {"due_at", "pending", "unit", "request", "occurrence"}
                }
                if type(details.get("number")) is int:
                    details["number"] = min(details["number"], 3)
                value = {**value, "details": details}
                self.db.execute(
                    "UPDATE alerts SET payload=? WHERE profile=? AND category=? AND subject=?",
                    (json.dumps(value, ensure_ascii=False), profile, category, subject),
                )
            if not value["active"]:
                self.history.save("alert", str(value["alert_id"]), value)
                self.db.execute(
                    "DELETE FROM alerts WHERE profile=? AND category=? AND subject=?",
                    (profile, category, subject),
                )
        for name, start, end, key, checked, ended in self.db.execute(
            "SELECT * FROM activity_windows WHERE ended_at IS NOT NULL"
        ).fetchall():
            self.history.save(
                "activity_window",
                f"{name}:{start}",
                {
                    "name": name,
                    "starts_at": start,
                    "ends_at": end,
                    "activity_key": key,
                    "checked_at": checked,
                    "ended_at": ended,
                },
            )
        self.db.execute("DELETE FROM activity_windows WHERE ended_at IS NOT NULL")
        self.prune_configurations()

    def cache_cleanup_blocked(self) -> bool:
        """Unresolved/frozen attempts retain their complete dependency and recovery graph."""
        return self.db.execute("SELECT 1 FROM attempts LIMIT 1").fetchone() is not None

    def prune_configurations(self) -> None:
        # Preparing explicit operations may still own a configuration in memory.
        if self.db.execute(
            "SELECT 1 FROM operations WHERE status NOT IN ('succeeded','failed','cancelled','blocked','recovery_required') LIMIT 1"
        ).fetchone():
            return
        pinned = {
            r[0]
            for r in self.db.execute(
                "SELECT revision FROM configuration_head UNION SELECT revision FROM attempts UNION SELECT revision FROM scheduled_runs"
            )
        }
        pinned.update(r["revision"] for r in self.scheduled_retries())
        with self.db:
            for revision, content in self.db.execute(
                "SELECT revision,content FROM configurations"
            ).fetchall():
                if revision not in pinned:
                    self.history.save(
                        "configuration", revision, {"content": content.decode("utf-8")}
                    )
                    self.db.execute("DELETE FROM configurations WHERE revision=?", (revision,))

    def _archive_schedule(self, identifier: str) -> None:
        value = self.scheduled_run(identifier)
        if value is not None:
            self.history.save("schedule", identifier, value)
            self.db.execute("DELETE FROM scheduled_runs WHERE id=?", (identifier,))

    def _archive_operation(self, identifier: str) -> None:
        value = self.operation(identifier)
        if value is not None:
            self.history.save("operation", identifier, value)
            self.db.execute("DELETE FROM operations WHERE id=?", (identifier,))

    def observe_activities(self, snapshot, *, now: float) -> None:
        from maa_planner.daemon.activity_state import observe_windows

        observe_windows(self.db, snapshot, now=now)

    def expire_activity_state(self, *, now: float) -> float | None:
        from maa_planner.daemon.activity_state import expire_state

        deadline = expire_state(self.db, now=now, event=self.event)
        for alert in self.alerts("admin"):
            if alert["category"] != "activity_shop_navigation":
                continue
            details = alert["details"]
            if details.get("all_profiles_disabled") is not True:
                continue
            activity_id = details.get("activity_id")
            starts_at = details.get("activity_starts_at")
            if (
                not isinstance(activity_id, str)
                or type(starts_at) not in (int, float)
                or not math.isfinite(starts_at)
            ):
                continue
            if not self.shop_navigation_state(
                {
                    "activity_id": activity_id,
                    "available_from": starts_at,
                }
            ):
                self.observe_alert("admin", alert["category"], alert["subject"], None, now=now)
        return deadline

    def has_shop_state(self, profile: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM activity_shops WHERE profile=? LIMIT 1", (profile,)
            ).fetchone()
            is not None
        )

    def shop_state(self, profile: str, activity_id: str, fingerprint: str) -> dict:
        row = self.db.execute(
            "SELECT payload FROM activity_shops WHERE profile=? AND activity_id=? AND fingerprint=?",
            (profile, activity_id, fingerprint),
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def shop_navigation_state(self, plan: dict) -> dict:
        row = self.db.execute(
            "SELECT payload FROM activity_shop_navigation WHERE activity_id=? AND starts_at=?",
            (plan["activity_id"], plan["available_from"]),
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def record_shop(self, profile: str, plan: dict, report: dict, *, attempt_id: str) -> None:
        from maa_planner.daemon.planning.activity_shop import shop_eligibility

        if (
            report.get("activity_id") != plan["activity_id"]
            or report.get("fingerprint") != plan["fingerprint"]
        ):
            raise ValueError("商店报告与固定活动范围不匹配")
        observed = report.get("observed_at")
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isfinite(observed)
        ):
            raise ValueError("商店报告缺少有效观测时间")
        ready = (
            shop_eligibility(plan, self.completed_stages(profile, plan["activity_id"]), observed)
            == "ready"
        )
        completed = (
            ready
            and report.get("outcome") == "completed"
            and report.get("reason") == "unlimited_commodity"
            and bool(report.get("evidence"))
        )
        payload = {
            **report,
            "completed": completed,
            "attempt_id": attempt_id,
            "profile": profile,
            "state_expires_at": plan["available_until"],
            "activity_name": plan["name"],
            "activity_starts_at": plan["available_from"],
        }
        if report.get("outcome") == "skipped":
            self.event("activity_shop_result", payload)
            return
        navigation = report.get("navigation_outcome", "not_attempted")
        if navigation not in {"not_attempted", "succeeded", "failed"}:
            raise ValueError("商店报告导航结果无效")
        first_navigation = None
        with self.db:
            if (
                plan["available_from"] <= observed < plan["available_until"]
                and navigation != "not_attempted"
                and not self.shop_navigation_state(plan)
            ):
                first_navigation = {
                    "status": "disabled" if navigation == "failed" else "succeeded",
                    "profile": profile,
                    "attempt_id": attempt_id,
                    "reason": report.get("reason"),
                    "navigation_phase": report.get("navigation_phase"),
                    "task_id": report.get("task_id"),
                    "observed_at": observed,
                    "state_expires_at": plan["available_until"],
                    "activity_name": plan["name"],
                    "activity_starts_at": plan["available_from"],
                }
                self.db.execute(
                    "INSERT INTO activity_shop_navigation VALUES (?,?,?)",
                    (
                        plan["activity_id"],
                        plan["available_from"],
                        json.dumps(first_navigation, ensure_ascii=False),
                    ),
                )
            self.db.execute(
                "INSERT INTO activity_shops VALUES (?,?,?,?) ON CONFLICT(profile,activity_id,fingerprint) DO UPDATE SET payload=excluded.payload",
                (
                    profile,
                    plan["activity_id"],
                    plan["fingerprint"],
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                ),
            )
            if first_navigation is not None and first_navigation["status"] == "disabled":
                self.observe_alert(
                    "admin",
                    "activity_shop_navigation",
                    f"{plan['activity_id']}:{plan['available_from']}",
                    {
                        **first_navigation,
                        "activity_id": plan["activity_id"],
                        "all_profiles_disabled": True,
                    },
                    now=observed,
                )
        self.event("activity_shop_result", payload)
        if navigation == "failed" and self.shop_navigation_state(plan).get("status") == "disabled":
            return
        self.observe_alert(
            profile,
            "activity_shop",
            plan["activity_id"],
            {"reason": report.get("reason"), "attempt_id": attempt_id}
            if report.get("outcome") == "unknown"
            or (report.get("outcome") == "completed" and not completed)
            else None,
            now=observed,
        )

    def completed_stages(self, profile: str, activity_id: str) -> frozenset[tuple[str, bool]]:
        return frozenset(
            (row[0], bool(row[1]))
            for row in self.db.execute(
                "SELECT stage_id, is_raid FROM stage_completions WHERE profile=? AND activity_id=?",
                (profile, activity_id),
            )
        )

    def stage_progress(self, profile: str, activity_id: str) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM stage_completions WHERE profile=? AND activity_id=? ORDER BY stage_id,is_raid",
                (profile, activity_id),
            )
        ]

    def confirm_stage(self, confirmation, *, now: float) -> dict:
        value = {**confirmation.to_dict(), "source": "api", "observed_at": now}
        with self.db:
            self.db.execute(
                "INSERT INTO stage_completions VALUES (?,?,?,?,?) "
                "ON CONFLICT(profile,activity_id,stage_id,is_raid) DO UPDATE SET payload=excluded.payload",
                (
                    confirmation.profile,
                    confirmation.activity_id,
                    confirmation.stage_id,
                    int(confirmation.is_raid),
                    json.dumps(value, ensure_ascii=False, allow_nan=False),
                ),
            )
        self.event("stage_confirmed", value)
        return value

    def unconfirm_stage(self, confirmation, *, now: float) -> dict:
        with self.db:
            removed = (
                self.db.execute(
                    "DELETE FROM stage_completions WHERE profile=? AND activity_id=? AND stage_id=? AND is_raid=?",
                    (
                        confirmation.profile,
                        confirmation.activity_id,
                        confirmation.stage_id,
                        int(confirmation.is_raid),
                    ),
                ).rowcount
                > 0
            )
        value = {**confirmation.to_dict(), "removed": removed, "source": "api", "observed_at": now}
        if removed:
            self.event("stage_unconfirmed", value)
        return value

    def record_clearance(
        self,
        profile: str,
        activity_id: str,
        report,
        *,
        now: float,
        attempt_id: str | None = None,
        log_directory: str | None = None,
        story_stages: frozenset[str] = frozenset(),
    ) -> None:
        """Record positive evidence only; a stopped phase returns to normal daily tasks."""
        from dataclasses import asdict

        if not profile or not activity_id:
            raise ValueError("通关记录必须指定账号和活动")
        known = self.completed_stages(profile, activity_id)
        with self.db:
            for result in report.results:
                if (
                    result.outcome != "completed"
                    or not result.evidence
                    or (
                        result.target.script_id == 0
                        and (result.target.stage_id not in story_stages or result.target.is_raid)
                    )
                ):
                    continue
                target = result.target
                if (target.stage_id, target.is_raid) in known:
                    continue
                self.db.execute(
                    "INSERT INTO stage_completions VALUES (?,?,?,?,?) "
                    "ON CONFLICT(profile,activity_id,stage_id,is_raid) DO UPDATE SET payload=excluded.payload",
                    (
                        profile,
                        activity_id,
                        target.stage_id,
                        int(target.is_raid),
                        json.dumps(
                            {
                                "target": profile,
                                "activity_id": activity_id,
                                "stage_id": target.stage_id,
                                "is_raid": target.is_raid,
                                "source": "story" if target.script_id == 0 else "copilot",
                                "observed_at": now,
                                "attempt_id": attempt_id,
                                "log_directory": log_directory,
                                "evidence": result.evidence[-(4 if target.script_id == 0 else 2) :],
                            },
                            ensure_ascii=False,
                            allow_nan=False,
                        ),
                    ),
                )
        self.event(
            "activity_copilot_result",
            {
                "profile": profile,
                "activity_id": activity_id,
                **asdict(report),
            },
        )
        if report.outcome == "cancelled":
            return
        warnings = {
            alert["subject"]: alert["details"]
            for alert in self.alerts(profile)
            if alert["category"] == "activity_copilot"
        }
        if report.warning is not None:
            warnings[activity_id] = report.warning
        elif report.outcome == "completed":
            warnings.pop(activity_id, None)
        self.reconcile_alerts(profile, "activity_copilot", warnings, now=now)

    def observe_alert(
        self, profile: str, category: str, subject: str, details: dict | None, *, now: float
    ) -> None:
        observations = {
            a["subject"]: a["details"] for a in self.alerts(profile) if a["category"] == category
        }
        if details is None:
            observations.pop(subject, None)
        else:
            observations[subject] = details
        self.reconcile_alerts(profile, category, observations, now=now)

    def alerts(self, profile: str | None = None, *, active_only: bool = True) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload FROM alerts ORDER BY profile, category, subject"
        ).fetchall()
        values = [json.loads(row[0]) for row in rows]
        if not active_only:
            resolved = {
                (v["profile"], v["category"], v["subject"]): v for v in self.history.values("alert")
            }
            resolved.update({(v["profile"], v["category"], v["subject"]): v for v in values})
            values = list(resolved.values())
        return [
            v
            for v in values
            if (profile is None or v["profile"] == profile) and (not active_only or v["active"])
        ]

    def reconcile_alerts(
        self, profile: str, category: str, observations: dict[str, dict], *, now: float
    ) -> None:
        """Reconcile only a successfully observed category, never missing data.

        Repeated observations update detail without re-emitting a notification.
        A new incident after recovery gets a new ID. Durable logs precede terminal cleanup.
        """
        if not math.isfinite(now) or any(not isinstance(v, dict) for v in observations.values()):
            raise ValueError("告警观测格式错误")
        json.dumps(observations, allow_nan=False)
        previous = {
            v["subject"]: v
            for v in self.alerts(profile, active_only=False)
            if v["category"] == category
        }
        transitions = []
        with self.db:
            for subject in sorted(previous.keys() | observations.keys()):
                old = previous.get(subject)
                present = subject in observations
                if not present and (old is None or not old["active"]):
                    continue
                transition = None
                if present:
                    if old is None or not old["active"]:
                        value = {
                            "alert_id": uuid.uuid4().hex,
                            "profile": profile,
                            "category": category,
                            "subject": subject,
                            "raised_at": now,
                        }
                        transition = "raised"
                    else:
                        value = dict(old)
                    value.update(
                        active=True, resolved_at=None, updated_at=now, details=observations[subject]
                    )
                else:
                    assert old is not None
                    value = {**old, "active": False, "resolved_at": now, "updated_at": now}
                    transition = "resolved"
                payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
                if value["active"]:
                    self.db.execute(
                        "INSERT INTO alerts VALUES (?,?,?,?) ON CONFLICT(profile, category, subject) DO UPDATE SET payload=excluded.payload",
                        (profile, category, subject, payload),
                    )
                else:
                    self.history.save("alert", str(value["alert_id"]), value)
                    self.db.execute(
                        "DELETE FROM alerts WHERE profile=? AND category=? AND subject=?",
                        (profile, category, subject),
                    )
                if transition:
                    transitions.append({**value, "transition": transition})
                    self.history.event("alert", {**value, "transition": transition})

        if self.on_event is not None:
            for value in transitions:
                self.on_event("alert", value)

    def close(self) -> None:
        self.db.close()

    def scheduled_retries(self) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT payload FROM scheduled_retries ORDER BY id")
        ]

    def save_scheduled_retry(self, payload: dict) -> None:
        if payload["status"] not in {"waiting", "running", "recovery_required"}:
            payload = {k: v for k, v in payload.items() if k not in {"due_at", "pending", "unit"}}
        with self.db:
            if payload["status"] in {"waiting", "running", "recovery_required"}:
                self.db.execute(
                    "INSERT INTO scheduled_retries VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (payload["id"], json.dumps(payload, ensure_ascii=False, allow_nan=False)),
                )
            else:
                self.history.save("retry", payload["id"], payload)
                self.db.execute("DELETE FROM scheduled_retries WHERE id=?", (payload["id"],))
            self.event("scheduled_retry", {k: v for k, v in payload.items() if k != "request"})

    def daily_controls(self):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT payload FROM daily_controls ORDER BY rowid")
        ]

    def daily_paused(self, profile):
        value = self.daily_control("pause:" + profile)
        return bool(value and value["paused"])

    def daily_control(self, identifier):
        row = self.db.execute(
            "SELECT payload FROM daily_controls WHERE id=?", (identifier,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_daily_control(self, value):
        with self.db:
            self.db.execute(
                "INSERT INTO daily_controls VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (value["id"], json.dumps(value, ensure_ascii=False)),
            )

    def archive_daily_control(self, value):
        self.history.save("daily_control", value["id"], value)
        with self.db:
            self.db.execute("DELETE FROM daily_controls WHERE id=?", (value["id"],))

    def accept_daily_control(self, identifier, payload, value, result, status):
        # No calls to store methods with their own `with self.db` in this transaction.
        with self.db:
            self.db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,NULL,0)",
                (
                    identifier,
                    json.dumps(payload, ensure_ascii=False),
                    status,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            self.db.execute(
                "INSERT INTO daily_controls VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (value["id"], json.dumps(value, ensure_ascii=False)),
            )
            if value["kind"] == "temporary":
                o = value["occurrence"]
                self.db.execute(
                    "INSERT INTO scheduled_runs VALUES (?,?,?,?,?,?, 'pending',NULL,NULL)",
                    (
                        value["id"],
                        o["schedule_id"],
                        o["profile"],
                        o["scheduled_at"],
                        o["expires_at"],
                        value["revision"],
                    ),
                )
        # Publication is deliberately after SQL commit. Failed publication leaves
        # the durable receipt and control together, available to idempotent retry.
        self.event("operation", {"operation_id": identifier, "status": status})
        if status in FINAL:
            with self.db:
                self._archive_operation(identifier)

    def claim_schedule(self, occurrence, revision: str) -> bool:
        with self.db:
            last = self.db.execute(
                "SELECT scheduled_at FROM schedule_cursors WHERE schedule_id=? AND profile=?",
                (occurrence.schedule_id, occurrence.profile),
            ).fetchone()
            if last and last[0] >= occurrence.scheduled_at:
                return False
            self.db.execute(
                "INSERT INTO schedule_cursors VALUES (?,?,?) ON CONFLICT(schedule_id,profile) DO UPDATE SET scheduled_at=excluded.scheduled_at",
                (occurrence.schedule_id, occurrence.profile, occurrence.scheduled_at),
            )
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO scheduled_runs VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL)",
                    (
                        occurrence.id,
                        occurrence.schedule_id,
                        occurrence.profile,
                        occurrence.scheduled_at,
                        occurrence.expires_at,
                        revision,
                    ),
                ).rowcount
                == 1
            )

    def scheduled_run(self, identifier: str) -> dict | None:
        row = self.db.execute("SELECT * FROM scheduled_runs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return self.history.get("schedule", identifier)
        return dict(
            zip(
                (
                    "id",
                    "schedule_id",
                    "profile",
                    "scheduled_at",
                    "expires_at",
                    "revision",
                    "status",
                    "attempt_id",
                    "error",
                ),
                row,
                strict=True,
            )
        )

    def schedule_update(self, identifier: str, status: str, *, attempt_id=None, error=None) -> None:
        with self.db:
            previous = self.scheduled_run(identifier)
            if previous:
                self.db.execute(
                    "INSERT OR IGNORE INTO scheduled_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    tuple(
                        previous[k]
                        for k in (
                            "id",
                            "schedule_id",
                            "profile",
                            "scheduled_at",
                            "expires_at",
                            "revision",
                            "status",
                            "attempt_id",
                            "error",
                        )
                    ),
                )
            self.db.execute(
                "UPDATE scheduled_runs SET status=?, attempt_id=COALESCE(?, attempt_id), error=? WHERE id=?",
                (status, attempt_id, error, identifier),
            )
            self.event("schedule", {"occurrence_id": identifier, "status": status, "error": error})
            if status in FINAL | {"skipped", "expired"}:
                self._archive_schedule(identifier)
        self.prune_configurations()

    def recover_schedules(self) -> None:
        for retry in self.scheduled_retries():
            archived = self.history.get("retry", retry["id"])
            if archived and archived["status"] not in {"waiting", "running", "recovery_required"}:
                self.save_scheduled_retry(archived)
                status = {"completed": "succeeded", "exhausted": "failed"}.get(
                    archived["status"], archived["status"]
                )
                self.schedule_update(
                    retry["id"],
                    status,
                    error="恢复重试达到上限" if archived["status"] == "exhausted" else None,
                )
            elif retry["status"] == "running":
                self.save_scheduled_retry({**retry, "status": "recovery_required"})
        active = {retry["id"] for retry in self.scheduled_retries()}
        for identifier, status, attempt_id in self.db.execute(
            "SELECT id,status,attempt_id FROM scheduled_runs"
        ).fetchall():
            archived = self.history.get("schedule", identifier)
            if (
                identifier not in active
                and archived
                and archived["status"] in FINAL | {"skipped", "expired"}
                and archived["attempt_id"] == attempt_id
            ):
                self.schedule_update(identifier, archived["status"], error=archived["error"])
            elif (
                status == "pending"
                and (control := self.daily_control(identifier))
                and (control["kind"] in {"postpone", "temporary"})
            ):
                continue
            elif status in {"pending", "preparing", "running"}:
                self.schedule_update(
                    identifier, "recovery_required", error="daemon 上次退出，日常任务不会自动重放"
                )

    def reserve_copilot(self, request_id: str, payload: dict, attempt_id: str) -> bool:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self.db:
            row = self.db.execute(
                "SELECT payload FROM copilot_requests WHERE id=?", (request_id,)
            ).fetchone()
            archived = self.history.get("copilot", request_id)
            if row is None and archived is not None:
                row = (json.dumps(archived["payload"], sort_keys=True, ensure_ascii=False),)
            if row is not None:
                if row[0] != serialized:
                    raise ValueError("request_id 已存在但 copilot 参数不同")
                return False
            self.db.execute(
                "INSERT INTO copilot_requests VALUES (?, ?, ?, NULL)",
                (request_id, serialized, attempt_id),
            )
            return True

    def copilot(self, request_id: str, payload: dict | None = None) -> dict | None:
        row = self.db.execute(
            "SELECT payload, attempt_id, error FROM copilot_requests WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            archived = self.history.get("copilot", request_id)
            if archived is None:
                return None
            row = (
                json.dumps(archived["payload"], sort_keys=True, ensure_ascii=False),
                archived["attempt_id"],
                archived["error"],
            )
        if payload is not None and json.loads(row[0]) != payload:
            raise ValueError("request_id 已存在但 copilot 参数不同")
        attempt = self.db.execute(
            "SELECT status, result FROM attempts WHERE id=?", (row[1],)
        ).fetchone()
        if attempt is None:
            archived = self.history.get("attempt", row[1])
            if archived:
                attempt = (archived["result"]["status"], json.dumps(archived["result"]))
        value = {"request_id": request_id, "attempt_id": row[1], "status": "preparing"}
        if row[2] is not None:
            value.update(status="failed", error=row[2])
        elif attempt is not None:
            value["status"] = attempt[0]
            if attempt[1] is not None:
                value["result"] = json.loads(attempt[1])
                value["result"].setdefault("event_shop_results", [])
                value["result"].setdefault("annihilation_results", [])
                value["result"].setdefault("task_results", [])
                value["result"].setdefault("activity_shop_results", [])
        return value

    def fail_copilot(self, request_id: str, error: str) -> None:
        with self.db:
            self.db.execute("UPDATE copilot_requests SET error=? WHERE id=?", (error, request_id))
            self._archive_copilot(request_id)

    def recover_copilot(self) -> None:
        with self.db:
            for identifier, attempt_id, error in self.db.execute(
                "SELECT id,attempt_id,error FROM copilot_requests WHERE attempt_id NOT IN (SELECT id FROM attempts)"
            ).fetchall():
                archived = self.history.get("attempt", attempt_id)
                if error is None and archived is None:
                    error = "daemon 上次退出，准备未完成；不会自动重放"
                    self.db.execute(
                        "UPDATE copilot_requests SET error=? WHERE id=?", (error, identifier)
                    )
                self._archive_copilot(identifier)
                if self.db.execute("SELECT 1 FROM operations WHERE id=?", (identifier,)).fetchone():
                    status = (
                        archived["result"]["status"] if archived and error is None else "failed"
                    )
                    self.operation_update(
                        identifier,
                        status,
                        result=archived["result"] if archived and error is None else None,
                        error=error,
                    )

    def _archive_copilot(self, identifier: str) -> None:
        row = self.db.execute(
            "SELECT payload,attempt_id,error FROM copilot_requests WHERE id=?", (identifier,)
        ).fetchone()
        if row:
            self.history.save(
                "copilot",
                identifier,
                {"payload": json.loads(row[0]), "attempt_id": row[1], "error": row[2]},
            )
            self.db.execute("DELETE FROM copilot_requests WHERE id=?", (identifier,))

    def configuration(self, revision: str, content: bytes) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO configurations VALUES (?, ?)", (revision, content)
            )
            self.db.execute("DELETE FROM configuration_head")
            self.db.execute("INSERT INTO configuration_head VALUES (?)", (revision,))
        self.prune_configurations()

    def configuration_content(self, revision: str) -> bytes:
        row = self.db.execute(
            "SELECT content FROM configurations WHERE revision=?", (revision,)
        ).fetchone()
        if row is None:
            archived = self.history.get("configuration", revision)
            if archived:
                return archived["content"].encode("utf-8")
            raise ValueError("执行配置快照不存在")
        return row[0]

    def prepared_request(self, request: RunRequest) -> None:
        with self.db:
            row = self.db.execute(
                "SELECT status, result FROM attempts WHERE id=?", (request.attempt_id,)
            ).fetchone()
            if row is None or row[0] != "preparing" or row[1] is not None:
                raise ValueError("执行已不在准备阶段")
            self.db.execute(
                "INSERT INTO prepared_executions VALUES (?, ?)",
                (
                    request.attempt_id,
                    json.dumps(request.to_dict(), sort_keys=True, ensure_ascii=False),
                ),
            )

    def prepared_runtime(self, attempt_id: str) -> dict | None:
        request = self.prepared_execution(attempt_id)
        if request is None:
            return None
        value = request.to_dict()
        return value.get("core", value.get("cli", {})).get("runtime")

    def prepared_execution(self, attempt_id: str) -> RunRequest | None:
        row = self.db.execute(
            "SELECT payload FROM prepared_executions WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row:
            return RunRequest.from_dict(json.loads(row[0]))
        archived = self.history.get("attempt", attempt_id)
        return (
            RunRequest.from_dict(archived["prepared"])
            if archived and archived.get("prepared")
            else None
        )

    def enqueue(self, request: RunRequest) -> bool:
        payload = json.dumps(request.to_dict(), sort_keys=True, ensure_ascii=False)
        with self.db:
            archived = self.history.get("attempt", request.attempt_id)
            if archived:
                if archived["request"] != request.to_dict():
                    raise ValueError("attempt ID 已存在但请求不同")
                return False
            row = self.db.execute(
                "SELECT request FROM attempts WHERE id=?", (request.attempt_id,)
            ).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise ValueError("attempt ID 已存在但请求不同")
                return False
            self.db.execute(
                "INSERT INTO attempts VALUES (?, ?, 'queued', NULL, ?)",
                (request.attempt_id, payload, request.config_revision),
            )
            return True

    def start(self, attempt_id: str) -> None:
        with self.db:
            cursor = self.db.execute(
                "UPDATE attempts SET status='preparing' WHERE id=? AND status='queued'",
                (attempt_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("attempt 不处于 queued 状态")

    def phase(self, attempt_id: str, phase: str) -> None:
        if phase not in {"ready", "admitted", "running", "finalizing"}:
            raise ValueError("未知执行阶段")
        with self.db:
            cursor = self.db.execute(
                "UPDATE attempts SET status=? WHERE id=? AND result IS NULL", (phase, attempt_id)
            )
            if cursor.rowcount != 1:
                raise ValueError("attempt 已结束或不存在")

    def finish(self, result: RunResult) -> None:
        from dataclasses import asdict

        if result.status not in TERMINAL:
            raise ValueError("未知终态")
        value = asdict(result)
        # JSON normalizes tuple/list representations across process boundaries.
        value = json.loads(json.dumps(value, ensure_ascii=False))
        with self.db:
            row = self.db.execute(
                "SELECT request,result FROM attempts WHERE id=?", (result.attempt_id,)
            ).fetchone()
            archived = self.history.get("attempt", result.attempt_id)
            previous = (
                json.loads(row[1])
                if row and row[1]
                else archived.get("result")
                if archived
                else None
            )
            if previous is not None:
                previous = json.loads(json.dumps(asdict(RunResult.from_dict(previous))))
            if previous is not None and previous != value:
                raise ValueError("attempt 已有其他结果")
            if row is None:
                if archived:
                    return
                raise ValueError("attempt 不存在")
            prepared = self.prepared_execution(result.attempt_id)
            self.history.save(
                "attempt",
                result.attempt_id,
                {
                    "request": json.loads(row[0]),
                    "prepared": prepared.to_dict() if prepared else None,
                    "result": value,
                },
            )
            if result.status in FINAL:
                for (identifier,) in self.db.execute(
                    "SELECT id FROM copilot_requests WHERE attempt_id=?", (result.attempt_id,)
                ).fetchall():
                    self._archive_copilot(identifier)
                self.db.execute(
                    "DELETE FROM prepared_executions WHERE attempt_id=?", (result.attempt_id,)
                )
                self.db.execute("DELETE FROM attempts WHERE id=?", (result.attempt_id,))
            else:
                self.db.execute(
                    "UPDATE attempts SET status=?,result=? WHERE id=?",
                    (result.status, json.dumps(value, ensure_ascii=False), result.attempt_id),
                )
        self.prune_configurations()

    def recover_attempts(self) -> None:
        """Finish durable results whose SQL cleanup did not commit; never replay work."""
        for identifier in self.unfinished():
            archived = self.history.get("attempt", identifier)
            result = (
                RunResult.from_dict(archived["result"])
                if archived
                else RunResult(
                    identifier, "recovery_required", "daemon 上次异常退出，需核对恢复日志"
                )
            )
            self.finish(result)

    def unfinished(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT id FROM attempts WHERE result IS NULL")]

    def results(self) -> list[RunResult]:
        values = {v["result"]["attempt_id"]: v["result"] for v in self.history.values("attempt")}
        values.update(
            {
                v["attempt_id"]: v
                for (payload,) in self.db.execute(
                    "SELECT result FROM attempts WHERE result IS NOT NULL"
                )
                if (v := json.loads(payload))
            }
        )
        return [RunResult.from_dict(v) for v in values.values()]

    def operation(self, identifier: str) -> dict | None:
        row = self.db.execute(
            "SELECT payload, status, result, error, cancel_requested FROM operations WHERE id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            return self.history.get("operation", identifier)
        return {
            "operation_id": identifier,
            **json.loads(row[0]),
            "status": row[1],
            "result": json.loads(row[2]) if row[2] is not None else None,
            "error": row[3],
            "cancel_requested": bool(row[4]),
        }

    def find_operation_key(self, key):
        row = self.db.execute(
            "SELECT id FROM operations WHERE json_extract(payload, '$.idempotency_key')=?", (key,)
        ).fetchone()
        if row is not None:
            return row[0]
        for operation in self.history.values("operation"):
            if operation.get("idempotency_key") == key:
                return operation["operation_id"]
        return None

    def unfinished_batches(self) -> list[str]:
        return [
            identifier
            for identifier, payload in self.db.execute(
                "SELECT id,payload FROM operations WHERE status NOT IN ("
                + ",".join("?" for _ in TERMINAL)
                + ")",
                tuple(TERMINAL),
            )
            if json.loads(payload).get("action") == "batch"
        ]

    def reserve_operation(self, identifier: str, payload: dict) -> bool:
        previous = self.operation(identifier)
        if previous is not None:
            if any(previous[key] != value for key, value in payload.items() if key != "expires_at"):
                raise APIError("conflict", "去重键已存在但参数不同", {"operation_id": identifier})
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO operations VALUES (?, ?, 'preparing', NULL, NULL, 0)",
                (identifier, json.dumps(payload, sort_keys=True, ensure_ascii=False)),
            )
            self.event("operation", {"operation_id": identifier, "status": "preparing"})
        return True

    def operation_update(self, identifier: str, status: str, *, result=None, error=None) -> None:
        with self.db:
            previous = self.operation(identifier)
            if previous:
                payload = {
                    k: v
                    for k, v in previous.items()
                    if k not in {"operation_id", "status", "result", "error", "cancel_requested"}
                }
                self.db.execute(
                    "INSERT OR IGNORE INTO operations VALUES (?,?,?,?,?,?)",
                    (
                        identifier,
                        json.dumps(payload, ensure_ascii=False),
                        previous["status"],
                        json.dumps(previous["result"]) if previous["result"] is not None else None,
                        previous["error"],
                        int(previous["cancel_requested"]),
                    ),
                )
            self.db.execute(
                "UPDATE operations SET status=?, result=?, error=? WHERE id=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    identifier,
                ),
            )
            self.event("operation", {"operation_id": identifier, "status": status})
            if status in FINAL:
                self._archive_operation(identifier)
        self.prune_configurations()

    def request_cancellation(self, identifier: str) -> None:
        with self.db:
            self.db.execute("UPDATE operations SET cancel_requested=1 WHERE id=?", (identifier,))
            self.event("cancel_requested", {"operation_id": identifier})

    def recover_operations(self) -> None:
        for (identifier,) in self.db.execute("SELECT id FROM operations").fetchall():
            current = self.operation(identifier)
            assert current is not None
            archived = self.history.get("operation", identifier)
            if archived and archived["status"] in FINAL:
                self.operation_update(
                    identifier,
                    archived["status"],
                    result=archived["result"],
                    error=archived["error"],
                )
            elif current["status"] in FINAL:
                with self.db:
                    self._archive_operation(identifier)
            elif current["action"] in {"daily-run", "daily-retry"} and self.daily_control(
                (current.get("result") or {}).get("occurrence_id", "")
            ):
                continue
            elif current["status"] not in TERMINAL:
                delegated = (
                    self.copilot(identifier) if current["action"] in {"copilot", "run"} else None
                )
                if delegated and delegated["status"] in TERMINAL:
                    self.operation_update(
                        identifier,
                        delegated["status"],
                        result=delegated.get("result"),
                        error=delegated.get("error"),
                    )
                else:
                    self.operation_update(
                        identifier,
                        "recovery_required",
                        error="daemon 上次退出，操作未完成；不会自动重放",
                    )

    def events(self, after: int, limit: int = 100) -> list[dict]:
        return self.history.events(after, limit)

    def event(self, kind: str, data: dict) -> None:
        self.history.event(kind, data)
        if self.on_event is not None:
            self.on_event(kind, data)

    def mark_game_resources_pending(self, profile: str, metadata: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO game_resource_pending VALUES (?,?) ON CONFLICT(profile) DO UPDATE SET metadata=excluded.metadata",
                (profile, json.dumps(metadata, ensure_ascii=False)),
            )

    def pending_game_resources(self) -> dict[str, dict]:
        return {
            profile: json.loads(metadata)
            for profile, metadata in self.db.execute("SELECT * FROM game_resource_pending")
        }

    def clear_game_resources_pending(self, profile: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM game_resource_pending WHERE profile=?", (profile,))

    def maintenance_login(self) -> dict | None:
        row = self.db.execute("SELECT payload FROM maintenance_login WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save_maintenance_login(self, fact: dict | None) -> None:
        with self.db:
            self.db.execute("DELETE FROM maintenance_login")
            if fact is not None:
                self.db.execute(
                    "INSERT INTO maintenance_login VALUES (1,?)",
                    (json.dumps(fact, ensure_ascii=False),),
                )
