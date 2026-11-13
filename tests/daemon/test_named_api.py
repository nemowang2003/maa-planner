"""Public RPC behavior through the real isolated daemon and client."""

import asyncio
from types import SimpleNamespace

import pytest

from maa_planner.daemon.api import ControlAPI
from maa_planner.daemon.notifications import NotificationHub
from maa_planner.daemon.operations import OperationService
from maa_planner.daemon.state import StateStore
from maa_planner.schema import METHODS, APIError
from tests.helpers.daemon import running


@pytest.mark.parametrize("method", ["submit", "batch.submit", "alerts", "events", "stage.progress"])
def test_removed_methods_are_rejected_without_accepting_work(tmp_path, monkeypatch, method):
    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            before = daemon.store.db.total_changes
            with pytest.raises(APIError) as failure:
                await client.call(method)
            assert failure.value.code == "invalid_argument"
            assert failure.value.details == {"method": method}
            assert daemon.store.db.total_changes == before
            assert not daemon.operations.tasks and not daemon.operations.submissions

    asyncio.run(scenario())


def test_catalogue_can_be_inspected_without_preparing_runtime(tmp_path, monkeypatch):
    async def scenario():
        async with running(tmp_path, monkeypatch) as (daemon, client, _):
            catalogue = await client.call("api.describe")
            assert set(catalogue) == set(METHODS)
            assert catalogue["copilot.run"]["required"] == ["idempotency_key", "profile", "url"]
            assert catalogue["execution.batch"]["optional"] == []
            assert catalogue["runtime.ensure"]["kind"] == "operation"
            assert not daemon.preparation_status

    asyncio.run(scenario())


def test_query_does_not_write_batch_completion_or_emit_events(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        store.reserve_operation("child", {"action": "run", "target": "nemo", "arguments": {}})
        store.operation_update("child", "succeeded")
        store.reserve_operation("parent", {"action": "batch", "target": None, "arguments": {}})
        store.operation_update("parent", "running", result={"operations": ["child"]})
        operations = OperationService(
            store=lambda: store,
            configuration=None,
            execution=None,
            observations=None,
            notifications=NotificationHub(),
            wakeup=lambda: None,
            update_game=None,
        )
        api = ControlAPI(
            operations=operations, control=None, notifications=operations.notifications
        )
        before, events = store.db.total_changes, store.events(0, 100)
        result = asyncio.run(
            api.handle({"method": "operation.get", "params": {"operation_id": "parent"}})
        )
        assert result["result"]["status"] == "succeeded"
        assert result["result"]["method"] == "execution.batch"
        assert "action" not in result["result"] and "target" not in result["result"]
        assert store.db.total_changes == before and store.events(0, 100) == events
        parent = store.operation("parent")
        assert parent is not None and parent["status"] == "running"
        operations.refresh_batches()
        parent = store.operation("parent")
        assert parent is not None and parent["status"] == "succeeded"
    finally:
        store.close()


def test_unknown_operation_has_structured_error_and_key_lookup_can_be_empty(tmp_path, monkeypatch):
    async def scenario():
        async with running(tmp_path, monkeypatch) as (_, client, _):
            assert await client.call("operation.find", idempotency_key="not-accepted") is None
            with pytest.raises(APIError) as failure:
                await client.call("operation.get", operation_id="missing")
            assert failure.value.code == "not_found"
            assert failure.value.details == {"method": "operation.get", "operation_id": "missing"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,params,action,arguments",
    [
        ("copilot.run", {"profile": "nemo", "url": "prts://1"}, "copilot", {"url": "prts://1"}),
        (
            "execution.run",
            {"profile": "nemo", "plan": {"tasks": [{"type": "Award"}]}},
            "run",
            {"plan": {"tasks": [{"type": "Award"}]}},
        ),
        (
            "schedule.plan.preview",
            {"profile": "nemo", "schedule_id": "morning"},
            "plan",
            {"schedule_id": "morning"},
        ),
        ("runtime.ensure", {}, "runtime-check", {}),
        ("resource.prepare_and_check", {}, "resource-check", {}),
        ("game.update", {"profile": "nemo"}, "game-update", {}),
    ],
)
def test_named_method_translates_only_at_application_boundary(method, params, action, arguments):
    accepted = []

    async def accept(command, **options):
        accepted.append((command, options))
        return {
            "operation_id": "a" * 32,
            "action": action,
            "target": command["target"],
            "status": "preparing",
            "idempotency_key": options["idempotency_key"],
        }

    service = SimpleNamespace(accept=accept)
    api = ControlAPI(operations=service, control=None, notifications=None)
    result = asyncio.run(
        api.handle({"method": method, "params": {"idempotency_key": "client-key", **params}})
    )
    operation = result["result"]
    assert operation["operation_id"] != "client-key" and len(operation["operation_id"]) == 32
    assert operation["method"] == method and operation["profile"] == params.get("profile")
    command, options = accepted[0]
    assert command == {
        "action": action,
        "target": params.get("profile"),
        "arguments": arguments,
        "expires_at": None,
    }
    assert options == {"idempotency_key": "client-key"}


def test_all_declared_operations_have_an_application_route():
    from maa_planner.daemon.control_contract import OPERATION_ACTIONS

    assert {name for name, spec in METHODS.items() if spec.kind == "operation"} == {
        *OPERATION_ACTIONS,
        "execution.batch",
    }
