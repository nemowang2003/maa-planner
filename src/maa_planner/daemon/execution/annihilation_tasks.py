"""Private, delegation-only annihilation graph; never enters normal deployment."""

from maa_planner.daemon.planning.annihilation import is_annihilation_stage
from maa_planner.daemon.planning.private_tasks import home_navigation, inherited_task


def delegation_task(task: dict, index: int, *, current: dict | None = None) -> tuple[dict, dict]:
    params = task.get("params", {})
    stage = params.get("stage")
    if not is_annihilation_stage(stage):
        raise ValueError("全权委托任务需要剿灭关卡")
    times = params.get("times", 2**31 - 1)
    if type(times) is not int or not 0 <= times <= 2**31 - 1:
        raise ValueError("剿灭 times 必须为非负 int32")
    # Custom has no MedicineCounter plugin. Refuse unsupported expenditure
    # rather than silently ignoring a configured allowance or consuming it.
    if any(
        params.get(key, 0) != 0
        for key in ("medicine", "stone", "expiring_medicine", "medicine_expire_days")
    ):
        raise ValueError("全权委托 Custom 暂不支持自动恢复理智，请设置用药和源石数量为 0")
    if stage != "Annihilation" and stage not in {
        "Chernobog@Annihilation",
        "LungmenOutskirts@Annihilation",
        "LungmenDowntown@Annihilation",
    }:
        raise ValueError(f"尚不支持该剿灭导航: {stage}")
    prefix = f"Planner@Annihilation{index}"

    def node(name):
        return prefix + "@" + name

    def inherited(base, **fields):
        # Inherit recognition only; close every flow edge explicitly. In
        # particular StartButton1 must never reach StartButton2/UsePrts.
        return {
            **inherited_task(base),
            "next": [],
            "sub": [],
            "exceededNext": [],
            "onErrorNext": [node("Uncertain")],
            "reduceOtherTimes": [],
            **fields,
        }

    def dispatch(next_names, **fields):
        return {"algorithm": "JustReturn", "next": [node(n) for n in next_names], **fields}

    gate = [node(n) for n in ("Ready", "Activate", "NoProxy", "OrdinaryProxy", "Unavailable")]
    settlement = [node(n) for n in ("Record", "LevelUp", "Settled", "SettledGeneric", "Wait")]
    nodes = {
        **home_navigation(prefix),
        "Begin": dispatch(["Home", "Back", "RecoverDefeat", "RecoverSettlement"]),
        "Home": inherited("Home", action="DoNothing", next=[node("Terminal")]),
        "Terminal": inherited("Fight", sub=["Terminal-Entry"], next=[node("TerminalReady")]),
        "TerminalReady": inherited(
            "GoTerminalStopflag", roi=[0, 600, 1280, 120], action="DoNothing", next=[node("Open")]
        ),
        "Open": inherited("Annihilation", next=[node("Open"), *gate], postDelay=1500, maxTimes=5),
        "Ready": inherited(
            "UsePrts-AnnihilationSuccessCheck", action="DoNothing", next=[node("Start")]
        ),
        "Activate": inherited(
            "UsePrts-AnnihilationCheck",
            action="ClickSelf",
            postDelay=1000,
            next=gate,
            maxTimes=10,
            exceededNext=[node("Unavailable")],
        ),
        "NoProxy": inherited("Annihilation@UnableToAgent2", action="DoNothing", onErrorNext=[]),
        "OrdinaryProxy": inherited("UsePrtsSuccessCheck", action="DoNothing", onErrorNext=[]),
        "Start": inherited(
            "StartButton1",
            maxTimes=times,
            exceededNext=[node("Limit")],
            next=[node("Confirm"), node("SanityRequired"), node("StoneRequired")],
            reduceOtherTimes=[node("Wait") + "*120", node("Activate") + "*10"],
        ),
        "SanityRequired": inherited("UseMedicine", action="DoNothing", next=[node("CloseSanity")]),
        "StoneRequired": inherited("UseStone", action="DoNothing", next=[node("CloseSanity")]),
        "CloseSanity": inherited("CloseStonePage", next=[node("InsufficientSanity")]),
        "Confirm": inherited("AnnihilationConfirm", next=settlement),
        "Record": inherited("AnnihilationPrtsRecordConfirm", next=settlement),
        "LevelUp": inherited("CheckLevelUp", next=settlement, maxTimes=5),
        "Wait": dispatch(
            ["Record", "LevelUp", "Settled", "SettledGeneric", "Wait"],
            postDelay=1000,
            maxTimes=120,
            exceededNext=[node("Uncertain")],
        ),
        "Settled": inherited("EndOfActionAnnihilation", next=[node("Dismiss")]),
        "SettledGeneric": inherited("EndOfAction", next=[node("Dismiss")]),
        "Dismiss": inherited("ClickCorner", next=gate, postDelay=1500),
        **{
            name: {"algorithm": "JustReturn", "next": []}
            for name in ("Unavailable", "Uncertain", "Limit", "InsufficientSanity")
        },
    }
    if stage != "Annihilation":
        nodes["Open"]["next"] = [node("SelectBack")]
        nodes["SelectBack"] = inherited("AnnihilationReturn", next=[node("Switch")])
        nodes["Switch"] = inherited("AnnihilationSwitchButton", next=[node("Select")])
        nodes["Select"] = inherited(
            stage.removesuffix("Annihilation") + "AnnihilationStage", next=gate, postDelay=1500
        )
    if current is not None:
        if stage != "Annihilation":
            raise ValueError("当期剿灭保护不能用于常驻导航")
        nodes["Current"] = {
            "algorithm": "OcrDetect",
            "text": [current["name"]],
            "fullMatch": True,
            "roi": [0, 0, 1280, 720],
            "action": "DoNothing",
            "next": gate,
            "onErrorNext": [node("Uncertain")],
            "exceededNext": [],
        }
        nodes["Open"]["next"] = [node("Open"), node("Current")]
        nodes["Dismiss"]["next"] = [node("Current")]
    if times == 0 or params.get("enable") is False:
        entry = "Limit"
    else:
        entry = "Begin"
    return (
        {
            "name": task.get("name", "Annihilation"),
            "type": "Custom",
            "params": {"task_names": [node(entry)]},
            "_annihilation_stage": stage,
        },
        {node(name): value for name, value in nodes.items()},
    )
