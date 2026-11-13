"""Account-level manual entry intent, owned by the daily scheduler."""

import json
import time
import uuid

from maa_planner.schema import APIError


def check_observation(store, profile, evidence, *, manual_entry=None, clear=True):
    """A known production change must not be overwritten by a pre-change snapshot."""
    row = store.db.execute(
        "SELECT applied_at FROM orundum_observation_wait WHERE profile=?", (profile,)
    ).fetchone()
    observation = evidence.observation
    if row is None or observation is None:
        return
    if observation.store_ts >= row[0]:
        if clear:
            with store.db:
                store.db.execute("DELETE FROM orundum_observation_wait WHERE profile=?", (profile,))
    elif manual_entry is None and observation.mode != "orundum":
        raise ValueError("Skland 基建快照早于已确认的搓玉应用，等待新的实际观测，未切回旧模式")


class OrundumIntents:
    def __init__(self, scheduler):
        self.scheduler = scheduler

    @property
    def store(self):
        return self.scheduler.store

    def get(self, profile):
        row = self.store.db.execute(
            "SELECT payload FROM orundum_intents WHERE profile=?", (profile,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, value):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO orundum_intents VALUES (?,?) ON CONFLICT(profile) DO UPDATE SET payload=excluded.payload",
                (value["profile"], json.dumps(value, ensure_ascii=False)),
            )

    def register(self, settings, *, profile, key):
        store = self.store
        payload = {
            "action": "orundum-request",
            "target": profile,
            "arguments": {},
            "expires_at": None,
            "idempotency_key": key,
        }
        # Single writer, no await: duplicate resolution and both records commit together.
        with store.db:
            existing = store.find_operation_key(key)
            if existing:
                previous = store.operation(existing)
                if any(previous[k] != v for k, v in payload.items()):
                    raise APIError("conflict", "去重键已存在但参数不同")
                return previous
            account = next((p for p in settings.profiles if p.name == profile and p.enabled), None)
            if account is None:
                raise ValueError("未知或已禁用 profile")
            if not any(
                s.enabled
                and profile in s.profiles
                and any(
                    t.enabled
                    and t.task_type == "Infrast"
                    and t.strategy == "expert"
                    and "orundum" in dict(t.files)
                    for t in s.tasks
                )
                for s in settings.schedules
            ):
                raise ValueError("账号没有可用的 expert 基建搓玉配置")
            value = self.get(profile)
            if value is None:
                value = {
                    "intent_id": uuid.uuid4().hex,
                    "profile": profile,
                    "status": "pending",
                    "requested_at": time.time(),
                }
                store.db.execute(
                    "INSERT INTO orundum_intents VALUES (?,?)",
                    (profile, json.dumps(value, ensure_ascii=False)),
                )
            identifier = uuid.uuid4().hex
            store.db.execute(
                "INSERT INTO operations VALUES (?,?,'succeeded',?,NULL,0)",
                (
                    identifier,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(value, ensure_ascii=False),
                ),
            )
        # Terminal log is durable before deleting the operation row.
        store.operation_update(identifier, "succeeded", result=value)
        return store.operation(identifier)

    def manual_entry(self, profile):
        value = self.get(profile)
        return value["intent_id"] if value and value["status"] == "pending" else None

    def start(self, request, query):
        if request.core is None or request.attempt_id not in self.scheduler.attempts:
            return False
        plan = json.loads(request.core.tasks.document).get("infrast_orundum")
        if plan is None or query != {k: plan[k] for k in ("intent_id", "business_id")}:
            return False
        document = json.loads(request.core.tasks.document)
        try:
            index = document["business_ids"].index(plan["business_id"])
            task = document["tasks"][index]
            if task["type"] != "Infrast":
                return False
            name = task["params"]["filename"].removeprefix("@file:")
            native = json.loads(request.core.tasks.attachments)[name]
            from maa_planner.daemon.execution.orundum import production_targets

            if production_targets(native, task["params"]["plan_index"]) != plan["targets"]:
                return False
        except (KeyError, ValueError, TypeError):
            return False
        value = self.get(request.binding.profile.name)
        if value is None or value["intent_id"] != plan["intent_id"]:
            return False
        if value["status"] != "pending":
            return False
        self.save(
            {
                **value,
                "status": "applying",
                "occurrence_id": request.job_id,
                "attempt_id": request.attempt_id,
                "business_id": plan["business_id"],
            }
        )
        return True

    def applied(self, request, report):
        value = self.get(request.binding.profile.name)
        archived = self.store.history.get("orundum_intent", report.get("intent_id", ""))
        if archived and archived["status"] == "applied":
            if value and value["intent_id"] == archived["intent_id"]:
                self._clear_applied(archived)
            return
        if value is None:
            return
        if (
            value["status"] != "applying"
            or value.get("attempt_id") != request.attempt_id
            or value["intent_id"] != report.get("intent_id")
            or value.get("business_id") != report.get("business_id")
            or report.get("outcome") != "applied"
            or report.get("mode") != "orundum"
            or type(report.get("task_id")) is not int
            or not isinstance(report.get("evidence"), list)
            or len(report["evidence"]) != 6
            or any(not isinstance(ref, str) or not ref for ref in report["evidence"])
        ):
            raise ValueError("搓玉应用报告与正在执行的意图不匹配")
        complete = {**value, "status": "applied", "report": report, "applied_at": time.time()}
        self.store.history.save("orundum_intent", value["intent_id"], complete)
        self._clear_applied(complete)
        self.store.event("orundum_applied", complete)

    def _clear_applied(self, complete):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO orundum_observation_wait VALUES (?,?) ON CONFLICT(profile) DO UPDATE SET applied_at=max(applied_at,excluded.applied_at)",
                (complete["profile"], complete["applied_at"]),
            )
            self.store.db.execute(
                "DELETE FROM orundum_intents WHERE profile=?", (complete["profile"],)
            )

    def finished(self, result):
        for report in result.orundum_results:
            row = self.store.prepared_execution(result.attempt_id)
            if row is not None:
                self.applied(row, report)
        for profile, payload in self.store.db.execute("SELECT * FROM orundum_intents").fetchall():
            value = json.loads(payload)
            if value.get("attempt_id") == result.attempt_id and value["status"] == "applying":
                business = next(
                    (b for b in result.business_results if b["id"] == value["business_id"]), None
                )
                known = (
                    result.status != "recovery_required"
                    and business is not None
                    and business["status"] in {"completed", "error"}
                )
                self.save({**value, "status": "pending" if known else "uncertain"})

    def recover(self):
        for profile, payload in self.store.db.execute("SELECT * FROM orundum_intents").fetchall():
            value = json.loads(payload)
            archived = self.store.history.get("orundum_intent", value["intent_id"])
            if archived and archived["status"] == "applied":
                self._clear_applied(archived)
            elif value["status"] == "applying":
                attempt = self.store.history.get("attempt", value["attempt_id"])
                if attempt:
                    from maa_planner.daemon.execution.context import RunResult

                    self.finished(RunResult.from_dict(attempt["result"]))
                else:
                    self.save({**value, "status": "uncertain"})
