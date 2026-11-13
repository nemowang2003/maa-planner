"""Regional trailing stories need explicit viewing; intermediate plots belong to Core."""


def trailing_stories(catalog, activity_id):
    stages = catalog["stage_table"]["stages"]
    zones = {
        z for z, aid in catalog["activity_table"]["zoneToActivity"].items() if aid == activity_id
    }
    normal = {
        k: v
        for k, v in stages.items()
        if v.get("zoneId") in zones and v.get("difficulty") == "NORMAL"
    }
    children = {k: [] for k in normal}
    for identifier, stage in normal.items():
        for condition in stage.get("unlockCondition", []):
            parent = condition["stageId"]
            if parent in normal and normal[parent]["zoneId"] == stage["zoneId"]:
                children[parent].append(identifier)
    memo = {}

    def before_battle(identifier, visiting):
        if identifier in visiting:
            raise ValueError("活动剧情路径存在循环")
        if identifier in memo:
            return memo[identifier]
        outcomes = []
        for child in children[identifier]:
            outcomes.append(
                before_battle(child, visiting | {identifier})
                if normal[child].get("isStoryOnly") is True
                else True
            )
        memo[identifier] = any(outcomes)
        return memo[identifier]

    return tuple(
        k
        for k in sorted(normal)
        if normal[k].get("isStoryOnly") is True and not before_battle(k, set())
    )


def story_nodes(prefix, code):
    from maa_planner.daemon.planning.private_tasks import inherited_task

    def n(name):
        return prefix + "@" + name

    def closed(base, **fields):
        return inherited_task(
            base,
            **{"sub": [], "onErrorNext": [], "exceededNext": [], "reduceOtherTimes": [], **fields},
        )

    return {
        n("PlotBegin"): {"algorithm": "JustReturn", "next": [n("PlotPanel"), n("PlotRight")]},
        n("PlotRight"): closed(
            "FullStageNavigation", next=[n("PlotRight")], maxTimes=10, exceededNext=[n("PlotSeek")]
        ),
        n("PlotSeek"): {"algorithm": "JustReturn", "next": [n("PlotClick"), n("PlotLeft")]},
        n("PlotLeft"): closed("ChapterSlowlySwipeToTheLeft", next=[n("PlotSeek")], maxTimes=20),
        n("PlotClick"): closed("ClickStageName", text=[code], next=[n("PlotPanel")], maxTimes=1),
        n("PlotPanel"): closed(
            "ClickedCorrectStage",
            text=[code],
            roi=[230, 300, 260, 70],
            action="DoNothing",
            next=[n("PlotStart")],
        ),
        n("PlotStart"): closed("BattleStartPlot", next=[n("PlotSkip")], maxTimes=1),
        n("PlotSkip"): closed("SkipBattlePlot", next=[n("PlotSkipped")], maxTimes=1),
        n("PlotSkipped"): closed(
            "SkipBattlePlotConfirm", next=[n("PlotFinish")], postDelay=1500, maxTimes=1
        ),
        n("PlotFinish"): {"algorithm": "JustReturn", "next": [n("PlotEnd"), n("PlotReturned")]},
        n("PlotEnd"): closed("EndOfPlot", next=[n("PlotReturned")], postDelay=500, maxTimes=1),
        n("PlotReturned"): closed("ClickStageName", text=[code], action="DoNothing", next=[]),
    }


def add_story_targets(prepared, catalog, *, now):
    from maa_planner.daemon.planning.clearance import navigation_nodes
    from maa_planner.daemon.planning.navigation import activity_labels

    plan, _, resources = prepared
    stages = catalog["stage_table"]["stages"]
    tails = set(trailing_stories(catalog, plan["activity_id"]))
    required = {stage for stage, raid in plan["required"] if not raid}
    completed = {tuple(v) for v in plan["completed"]}
    mapping = catalog["activity_table"]["zoneToActivity"]
    raid_to_normal = {
        s["hardStagedId"]: k
        for k, s in stages.items()
        if s.get("difficulty") == "NORMAL" and s.get("hardStagedId")
    }

    def prerequisites(identifier, visiting):
        if identifier in visiting:
            raise ValueError("活动剧情前置路径存在循环")
        found = set()
        for condition in stages[identifier].get("unlockCondition", []):
            parent = condition["stageId"]
            if parent not in stages:
                raise ValueError("剧情前置关卡元数据缺失")
            stage = stages[parent]
            if mapping.get(stage.get("zoneId")) != plan["activity_id"]:
                continue
            if stage.get("isStoryOnly") is True and parent not in tails:
                found.update(tuple(v) for v in prerequisites(parent, visiting | {identifier}))
            else:
                if stage.get("difficulty") == "FOUR_STAR":
                    if parent not in raid_to_normal:
                        raise ValueError("剧情前置突袭关卡元数据缺失")
                    found.add((raid_to_normal[parent], True))
                else:
                    found.add((parent, False))
        return [list(v) for v in sorted(found)]

    present = {e["stage_id"] for e in plan["targets"]}
    for identifier in sorted(tails & required):
        if (identifier, False) in completed or identifier in present:
            continue
        stage = stages[identifier]
        prefix = f"Planner@Story{len(plan['targets'])}"
        zone = stage["zoneId"]
        name = catalog["zone_table"]["zones"][zone].get("zoneNameSecond")
        if not stage.get("code") or not isinstance(name, str) or not name:
            raise ValueError("拖尾剧情缺少可导航关卡名或区域")
        codes = [s["code"] for s in stages.values() if s.get("zoneId") == zone and s.get("code")]
        resources.update(
            navigation_nodes(
                prefix,
                plan["activity_name"],
                name,
                codes,
                activity_labels=activity_labels(catalog, plan["activity_id"]),
            )
        )
        resources.update(story_nodes(prefix, stage["code"]))
        plan["targets"].append(
            {
                "kind": "tail_story",
                "script_id": 0,
                "stage_id": identifier,
                "code": stage["code"],
                "is_raid": False,
                "game_stage_id": identifier,
                "script_stage_id": identifier,
                "navigation": prefix,
                "zone_id": zone,
                "prerequisites": prerequisites(identifier, set()),
            }
        )
    return prepared
