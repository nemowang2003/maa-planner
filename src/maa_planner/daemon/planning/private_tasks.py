"""Bind project-owned derived MatchTemplate nodes to existing Core PNGs.

Core intentionally defaults a derived task's template to the new task name;
baseTask alone does not retain the base task's template field.
"""

_HOME = ("SwitchTheme@ToggleSettingsMenu.png", "SwitchTheme@ToggleSettingsMenu-v2.png")
_TEMPLATES = {
    "Home": _HOME,
    "Fight": _HOME,
    "Return": ("Return.png", "Return-White.png"),
    "GoTerminalStopflag": "StageResource.png",
    "SS-Open": ("StageSideStory.png", "StageActivity.png"),
    "SS@Store@ClickItem": "SS@Store@ClickItem.png",
    "StageDrops-Stars-2": "StageDrops-Stars-2.png",
    "Annihilation": "StageAnnihilation.png",
    "UsePrts-AnnihilationSuccessCheck": "UsePrts-AnnihilationSuccess.png",
    "UsePrts-AnnihilationCheck": "UsePrts-Annihilation.png",
    "Annihilation@UnableToAgent2": "UnableToAgent2.png",
    "UsePrtsSuccessCheck": "UsePrtsSuccess.png",
    "UseMedicine": "UseMedicine.png",
    "UseStone": "UseStone.png",
    "AnnihilationConfirm": "AnnihilationConfirm.png",
    "AnnihilationPrtsRecordConfirm": "AnnihilationPrtsRecordConfirm.png",
    "CheckLevelUp": "CheckLevelUp.png",
    "EndOfActionAnnihilation": "EndOfActionAnnihilation.png",
    "EndOfAction": "EndOfAction.png",
    "BattleStartPlot": "BattleStartPlot.png",
    "SkipBattlePlot": "SkipThePreBattlePlot.png",
    "SkipBattlePlotConfirm": "SkipThePreBattlePlotConfirm.png",
    "EndOfPlot": "EndOfPlot.png",
    "AnnihilationReturn": "Return.png",
}


def inherited_task(base: str, **fields) -> dict:
    template = _TEMPLATES.get(base)
    return {
        "baseTask": base,
        **(
            {"template": list(template) if isinstance(template, tuple) else template}
            if template is not None
            else {}
        ),
        **fields,
    }


def activity_entry(name: str, labels: list[str]) -> dict:
    return inherited_task(
        "SS-OpenOcr", text=list(dict.fromkeys([name, *labels])), roi=[0, 0, 1280, 720]
    )


def home_navigation(prefix: str) -> dict:
    """Recognize and dismiss battle results before using either back-button theme."""

    def node(name):
        return prefix + "@" + name

    gate = [node(name) for name in ("Home", "Back", "RecoverDefeat", "RecoverSettlement")]

    def closed(base, **fields):
        return inherited_task(
            base, next=[], sub=[], onErrorNext=[], exceededNext=[], reduceOtherTimes=[], **fields
        )

    # Set flow edges separately so no inherited route can reach a battle start.
    nodes = {
        "Home": closed("Home"),
        "Back": closed("Return", maxTimes=12),
        "RecoverDefeat": closed("FightMissionFailed", action="ClickSelf", maxTimes=2),
        "RecoverSettlement": closed("EndOfAction", action="DoNothing", maxTimes=2),
        "DismissSettlement": closed("ClickCorner", maxTimes=2),
        "ToHome": {"algorithm": "JustReturn", "next": gate},
    }
    for name in ("Back", "RecoverDefeat", "DismissSettlement"):
        nodes[name]["next"] = gate
    nodes["RecoverSettlement"]["next"] = [node("DismissSettlement")]
    return nodes


def activity_page_navigation(
    prefix: str, checkpoint: str, entry: str | list[str], *, root="FromActivity"
) -> dict:
    """Reach a page from an owned activity context, with bounded backtracking."""

    def node(name):
        return prefix + "@" + name

    entries = [entry] if isinstance(entry, str) else entry
    candidates = [
        node(checkpoint),
        *(node(v) for v in entries),
        node("BackToActivity"),
    ]
    return {
        root: {"algorithm": "JustReturn", "next": candidates},
        "BackToActivity": inherited_task(
            "Return",
            maxTimes=4,
            exceededNext=[],
            onErrorNext=[],
            next=candidates,
        ),
    }
