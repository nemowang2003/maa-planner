"""Navigate and purchase only after full activity eligibility is established."""

import time

from maa_planner.daemon.planning.activity_shop import shop_eligibility


def run_shop(plan, completed, execute_tasks, results, stop, permission=None) -> dict:
    navigation_outcome = "not_attempted"
    navigation_phase = None
    navigation_status_before_attempt = plan.get("navigation_status", "untried")

    def report(outcome, reason, evidence=(), task_id=None):
        error = results.tasks.get(task_id, {}) if task_id is not None else {}
        return {
            **(
                {
                    "core_error": {
                        "status": error["status"],
                        "task_id": task_id,
                        "evidence": error.get("error_evidence"),
                    }
                }
                if error.get("status") in {"error", "failed"}
                else {}
            ),
            "activity_id": plan["activity_id"],
            "fingerprint": plan["fingerprint"],
            "after_task": plan.get("report_after_task", plan["after_task"]),
            "outcome": outcome,
            "reason": reason,
            "evidence": list(evidence),
            "task_id": task_id,
            "observed_at": time.time(),
            "navigation_outcome": navigation_outcome,
            "navigation_phase": navigation_phase,
            "navigation_status_before_attempt": navigation_status_before_attempt,
        }

    if plan.get("shop_completed", False) or plan["garbage"]:
        return report("skipped", "already_completed")
    if plan.get("navigation_status") == "disabled":
        return report("skipped", "navigation_disabled")
    eligibility = shop_eligibility(plan, completed, time.time())
    if eligibility != "ready":
        return report("skipped", eligibility)
    if not plan["entry"]:
        return report("unknown", "entry_missing")
    if permission is not None:
        status = permission(
            {
                "activity_id": plan["activity_id"],
                "fingerprint": plan["fingerprint"],
                "after_task": plan.get("report_after_task", plan["after_task"]),
            }
        )
        if status in {"disabled", "pending"}:
            return report("skipped", "navigation_" + status)
        if status not in {"untried", "succeeded"}:
            return report("unknown", "navigation_permission_unknown")
        navigation_status_before_attempt = status
    for phase in ("ToHome", "Terminal", "Open"):
        if stop.is_set():
            raise InterruptedError("商店导航期间取消")
        if shop_eligibility(plan, completed, time.time()) != "ready":
            return report("skipped", "activity_closed")
        navigation_phase = phase
        ids = execute_tasks(
            [
                {
                    "type": "Custom",
                    "name": f"{plan['key']}:shop:{phase}",
                    "params": {"task_names": [plan["navigation"] + "@" + phase]},
                }
            ],
            handled=True,
        )
        if not ids or results.tasks[ids[0]]["status"] != "completed":
            if stop.is_set():
                raise InterruptedError("商店导航期间取消")
            navigation_outcome = "failed"
            return report("unknown", "navigation_failed", task_id=ids[0] if ids else None)
        if phase == "Open" and not any(
            node == "ShopReady" for node, _ in results.nodes.get(ids[0], [])
        ):
            if stop.is_set():
                raise InterruptedError("商店导航期间取消")
            navigation_outcome = "failed"
            return report("unknown", "navigation_unverified", task_id=ids[0])
    navigation_outcome = "succeeded"
    if shop_eligibility(plan, completed, time.time()) != "ready":
        return report("skipped", "activity_closed")
    ids = execute_tasks(
        [
            {
                "type": "Custom",
                "name": f"{plan['key']}:shop:purchase",
                "params": {
                    "task_names": ["SS@Store@Begin"],
                    "event_shop": {"blacklist": plan["blacklist"]},
                },
            }
        ],
        handled=True,
    )
    if not ids:
        return report("unknown", "missing_shop_task")
    result = next((v for v in results.shop_results() if v["task_id"] == ids[0]), None)
    if stop.is_set() and (result is None or result["outcome"] != "completed"):
        raise InterruptedError("活动商店期间取消")
    if result is None:
        return report("unknown", "missing_shop_evidence", task_id=ids[0])
    return report(result["outcome"], result["reason"], result["evidence"], ids[0])
