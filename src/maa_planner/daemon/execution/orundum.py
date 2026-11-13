"""Reduce MaaCore's room production observations for one Infrast task."""

from collections import Counter

ALIASES = {
    "CombatRecord": "record",
    "MiddleBattleRecord": "record",
    "PureGold": "gold",
    "OriginStone": "shard",
    "OriginiumShard": "shard",
    "Money": "money",
    "SyntheticJade": "orundum",
}


CONFIG_PRODUCTS = {
    "Battle Record": "record",
    "Pure Gold": "gold",
    "Originium Shard": "shard",
    "LMD": "money",
    "Orundum": "orundum",
}


def production_targets(document, plan_index):
    try:
        rooms = document["plans"][plan_index]["rooms"]
        targets = []
        for name, facility in (("manufacture", "Mfg"), ("trading", "Trade")):
            if not isinstance(rooms[name], list):
                raise ValueError("制造／贸易配置必须是房间数组")
            for index, room in enumerate(rooms[name]):
                if room.get("skip", False) is not False:
                    raise ValueError("搓玉生产目标房间不能 skip")
                targets.append(
                    {
                        "facility": facility,
                        "index": index,
                        "product": CONFIG_PRODUCTS[room["product"]],
                    }
                )
    except (KeyError, TypeError, IndexError, AttributeError) as error:
        raise ValueError("搓玉文件所选子计划缺少明确的制造／贸易产物") from error
    mfg = Counter(t["product"] for t in targets if t["facility"] == "Mfg")
    trade = Counter(t["product"] for t in targets if t["facility"] == "Trade")
    if mfg != {"shard": 1, "gold": 2, "record": 1} or trade != {"money": 1, "orundum": 1}:
        raise ValueError("搓玉文件所选子计划必须为约定的 243 搓玉生产布局")
    return targets


class ProductionEvidence:
    def __init__(self, task_id, plan):
        self.task_id, self.plan = task_id, plan
        self.targets = {(t["facility"], t["index"]): t["product"] for t in plan["targets"]}
        self.observed = {}
        self.reported = False

    def consume(self, message, payload, reference):
        if (
            message != 20003
            or payload.get("taskchain") != "Infrast"
            or type(payload.get("taskid")) is not int
            or payload.get("taskid") != self.task_id
        ):
            return None
        what, details = payload.get("what"), payload.get("details", {})
        if not isinstance(details, dict):
            return None
        index = details.get("index")
        key = details.get("facility"), index
        if (
            type(index) is not int
            or not isinstance(details.get("facility"), str)
            or key not in self.targets
        ):
            return None
        if what == "ProductChangeFail":
            self.observed.pop(key, None)
        elif what in {"ProductOfFacility", "ProductChanged"}:
            raw = details.get("product")
            product = ALIASES.get(raw) if isinstance(raw, str) else None
            self.observed[key] = (product, reference)
        if (
            not self.reported
            and len(self.observed) == len(self.targets)
            and all(self.observed.get(k, (None,))[0] == v for k, v in self.targets.items())
        ):
            self.reported = True
            return {
                "intent_id": self.plan["intent_id"],
                "business_id": self.plan["business_id"],
                "task_id": self.task_id,
                "mode": "orundum",
                "outcome": "applied",
                "evidence": [self.observed[k][1] for k in self.targets],
            }
        return None


def validate_intent_plan(plan, document):
    if (
        not isinstance(plan, dict)
        or set(plan) != {"intent_id", "business_id", "targets"}
        or not isinstance(plan["intent_id"], str)
        or not plan["intent_id"]
        or not isinstance(plan["business_id"], str)
    ):
        raise ValueError("infrast_orundum 需要固定意图、业务与房间目标")
    ids = document.get("business_ids", [f"task:{i}" for i in range(len(document["tasks"]))])
    if (
        plan["business_id"] not in ids
        or document["tasks"][ids.index(plan["business_id"])]["type"] != "Infrast"
    ):
        raise ValueError("搓玉意图必须指向一个 Infrast 业务")
    expected_rooms = {("Mfg", i) for i in range(4)} | {("Trade", i) for i in range(2)}
    targets = plan["targets"]
    if (
        not isinstance(targets, list)
        or len(targets) != 6
        or any(
            not isinstance(t, dict)
            or set(t) != {"facility", "index", "product"}
            or not isinstance(t["facility"], str)
            or type(t["index"]) is not int
            or not isinstance(t["product"], str)
            for t in targets
        )
        or {(t["facility"], t["index"]) for t in targets} != expected_rooms
        or Counter(t["product"] for t in targets if t["facility"] == "Mfg")
        != {"shard": 1, "gold": 2, "record": 1}
        or Counter(t["product"] for t in targets if t["facility"] == "Trade")
        != {"money": 1, "orundum": 1}
    ):
        raise ValueError("搓玉意图房间目标必须为完整的 243 生产布局")
