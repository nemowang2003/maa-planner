"""Conversion between internal operation records and public wire data."""

# Internal execution commands may have different names from the public contract.
OPERATION_ACTIONS = {
    "daily.postpone": "daily-postpone",
    "daily.skip": "daily-skip",
    "daily.run": "daily-run",
    "daily.retry": "daily-retry",
    "daily.pause": "daily-pause",
    "execution.run": "run",
    "infrast.orundum.request": "orundum-request",
    "copilot.run": "copilot",
    "activity.fetch": "activities",
    "news.collect": "news",
    "assets.prepare": "assets",
    "account.context.fetch": "player-context",
    "schedule.plan.preview": "plan",
    "emulator.list": "emulators",
    "runtime.ensure": "runtime-check",
    "resource.prepare_and_check": "resource-check",
    "resource.update": "resource-update",
    "game.inspect": "game-check",
    "game.update": "game-update",
    "runtime.prepare": "runtime-prepare",
    "execution.recover": "recover",
}
ACTION_METHODS = {v: k for k, v in OPERATION_ACTIONS.items()} | {"batch": "execution.batch"}


def public_operation(operation):
    if operation is None:
        return None
    return {
        {"action": "method", "target": "profile", "expires_at": "latest_start_at"}.get(
            k, k
        ): ACTION_METHODS[v] if k == "action" else v
        for k, v in operation.items()
        if k != "arguments"
    }
