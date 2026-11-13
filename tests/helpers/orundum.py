"""Fixed native production plans and callback payloads for manual entry tests."""

from maa_planner.daemon.execution.orundum import production_targets


def native_plan(mode="orundum"):
    products = {
        "normal": ("Pure Gold", "Pure Gold", "Battle Record", "Battle Record"),
        "three_gold": ("Pure Gold", "Pure Gold", "Pure Gold", "Battle Record"),
        "orundum": ("Originium Shard", "Pure Gold", "Pure Gold", "Battle Record"),
        "sell_only": ("Pure Gold", "Pure Gold", "Battle Record", "Battle Record"),
    }
    return {
        "plans": [
            {
                "rooms": {
                    "manufacture": [{"product": p} for p in products[mode]],
                    "trading": [
                        {"product": p}
                        for p in (
                            ("LMD", "Orundum")
                            if mode in {"orundum", "sell_only"}
                            else ("LMD", "LMD")
                        )
                    ],
                }
            }
        ]
    }


def plan():
    return {
        "intent_id": "intent",
        "business_id": "task:0",
        "targets": production_targets(native_plan(), 0),
    }


def callback(target, *, task_id=7, what="ProductOfFacility", product=None):
    names = {
        "shard": "OriginStone",
        "gold": "PureGold",
        "record": "CombatRecord",
        "money": "Money",
        "orundum": "SyntheticJade",
    }
    return {
        "taskchain": "Infrast",
        "taskid": task_id,
        "what": what,
        "subtask": "InfrastMfgTask" if target["facility"] == "Mfg" else "InfrastTradeTask",
        "details": {
            "facility": target["facility"],
            "index": target["index"],
            "product": product or names[target["product"]],
        },
    }
