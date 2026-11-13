"""One on-demand rotation rescue, gated by live NoProxy and map/proxy evidence."""

import hashlib
import time
from pathlib import Path

from maa_planner.daemon.planning.annihilation_rescue import search_scripts
from maa_planner.daemon.planning.auto_copilot import resolve_formation
from maa_planner.daemon.tools.storage import write_json


def run_rescue(context, stage, execute_tasks, results, stop, *, directory):
    evidence = []

    def report(outcome, reason, **details):
        return {
            "outcome": outcome,
            "reason": reason,
            "evidence": list(evidence),
            "observed_at": time.time(),
            **details,
        }

    def execute(task):
        if stop.is_set():
            raise InterruptedError("剿灭补救取消")
        ids = execute_tasks([task], handled=True)
        if stop.is_set():
            raise InterruptedError("剿灭补救取消")
        if not ids:
            return None, []
        identifier = ids[0]
        nodes = results.nodes.get(identifier, [])
        return results.tasks[identifier], nodes

    def identify():
        matches = []
        for index, entry in enumerate(context["candidates"]):
            if not entry["available_from"] <= time.time() < entry["available_until"]:
                continue
            name = f"Stage{index}"
            task, nodes = execute(
                {"type": "Custom", "params": {"task_names": ["Planner@Rescue@" + name]}}
            )
            proof = [ref for node, ref in nodes if node == name]
            if task and task["status"] == "completed" and proof:
                matches.append(entry)
                evidence.extend(proof)
        return matches

    if stage != "Annihilation":
        return report("unavailable", "not_current_rotation")
    matches = identify()
    if len(matches) != 1:
        return report("unknown", "map_not_unique")
    entry = matches[0]
    details = {"game_stage_id": entry["stage_id"], "script_id": None}
    rejected = []
    try:
        scripts = search_scripts(Path(directory) / "annihilation-source", entry, stop)
        selected = None
        for identifier, script in scripts:
            try:
                fixed, _ = resolve_formation(script, frozenset(context["owned_operators"]), 0)
            except ValueError as error:
                rejected.append({"script_id": identifier, "reason": str(error)})
                continue
            selected = identifier, fixed
            break
    except InterruptedError:
        raise
    except Exception as error:
        if stop.is_set():
            raise InterruptedError("剿灭作业搜索取消") from error
        return report("unknown", "copilot_search_failed", error=str(error), **details)
    if selected is None:
        return report("unavailable", "no_usable_copilot", rejected=rejected, **details)
    identifier, script = selected
    filename = Path(directory) / "annihilation-rescue.json"
    write_json(filename, script)
    details["script_id"] = identifier
    details["script_sha256"] = hashlib.sha256(filename.read_bytes()).hexdigest()
    if not entry["available_from"] <= time.time() < entry["available_until"]:
        return report("unavailable", "map_closed", **details)
    task, _ = execute(
        {
            "type": "Copilot",
            "name": "annihilation:rescue",
            "params": {
                "filename": str(filename),
                "formation": True,
                "ignore_requirements": False,
                "support_unit_usage": 0,
                "use_sanity_potion": False,
            },
        }
    )
    if not task or task["status"] != "completed":
        return report("unknown", "copilot_failed", **details)
    task, nodes = execute(
        {
            "type": "Custom",
            "name": "annihilation:verify",
            "params": {"task_names": ["Planner@Rescue@Wait"]},
        }
    )
    settlement = [ref for name, ref in nodes if name in {"Settled", "SettledGeneric"}]
    proxy = []
    settled = False
    for name, reference in nodes:
        if name in {"Settled", "SettledGeneric"}:
            settled = True
        elif name == "Ready" and settled:
            proxy.append(reference)
    evidence.extend(settlement + proxy)
    if not task or task["status"] != "completed" or not settlement or not proxy:
        return report("unknown", "settlement_or_delegation_unverified", **details)
    after = identify()
    if len(after) != 1 or after[0]["stage_id"] != entry["stage_id"]:
        return report("unknown", "post_battle_map_mismatch", **details)
    if not entry["available_from"] <= time.time() < entry["available_until"]:
        return report("unavailable", "map_closed", **details)
    return report("verified", "settlement_and_delegation_ready", **details)
