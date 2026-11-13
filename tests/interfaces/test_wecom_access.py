import asyncio

import pytest

from maa_planner.interfaces.wecom.access import AccessDenied, AccessPolicy
from maa_planner.schema import Request
from tests.helpers.wecom import Peer, config


@pytest.mark.parametrize(
    "method,params",
    [
        (
            "execution.run",
            {"idempotency_key": "one", "profile": "wyf", "plan": {"tasks": [{"type": "Award"}]}},
        ),
        ("alert.list", {"profile": "wyf"}),
        ("alert.list", {"profile": "admin"}),
        ("stage.confirm", {"profile": "wyf", "activity_id": "a", "stage_id": "s"}),
        ("stage.progress.get", {"profile": "wyf", "activity_id": "a"}),
        ("daemon.stop", {"force": True}),
        ("daemon.restart", {}),
        ("daemon.status", {}),
        ("config.get", {}),
        ("config.replace", {"toml": "test"}),
        ("event.list", {}),
        ("activity.fetch", {"idempotency_key": "one"}),
    ],
)
def test_non_admin_cannot_invoke_other_accounts_or_global_methods(tmp_path, method, params):
    async def scenario():
        peer = Peer()
        with pytest.raises(AccessDenied):
            await AccessPolicy(config(tmp_path)).execute("alice", Request(method, params), peer)
        assert not peer.calls

    asyncio.run(scenario())


def test_batch_requires_every_member_to_be_in_scope(tmp_path):
    async def scenario():
        peer = Peer()
        jobs = [
            {"method": "execution.run", "profile": name, "plan": {"tasks": [{"type": "Award"}]}}
            for name in ("nemo", "wyf")
        ]
        with pytest.raises(AccessDenied):
            await AccessPolicy(config(tmp_path)).execute(
                "alice",
                Request("execution.batch", {"idempotency_key": "batch", "jobs": jobs}),
                peer,
            )
        assert not peer.calls

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["operation.get", "operation.cancel", "operation.find"])
def test_foreign_operation_is_inspected_but_never_returned_or_cancelled(tmp_path, method):
    async def scenario():
        peer = Peer(
            {
                "foreign": {
                    "method": "execution.run",
                    "profile": "wyf",
                    "result": "private",
                    "idempotency_key": "foreign-key",
                }
            }
        )
        with pytest.raises(AccessDenied):
            await AccessPolicy(config(tmp_path)).execute(
                "alice",
                Request(
                    method,
                    {"idempotency_key": "foreign-key"}
                    if method == "operation.find"
                    else {"operation_id": "foreign"},
                ),
                peer,
            )
        assert [call["method"] for call in peer.calls] == [
            "operation.find" if method == "operation.find" else "operation.get"
        ]

    asyncio.run(scenario())


def test_batch_cancel_checks_all_children_before_mutating(tmp_path):
    async def scenario():
        peer = Peer(
            {
                "execution.batch": {
                    "method": "execution.batch",
                    "result": {"operations": ["a", "b"]},
                },
                "a": {"method": "execution.run", "profile": "nemo"},
                "b": {"method": "copilot.run", "profile": "wyf"},
            }
        )
        with pytest.raises(AccessDenied):
            await AccessPolicy(config(tmp_path)).execute(
                "alice", Request("operation.cancel", {"operation_id": "execution.batch"}), peer
            )
        assert all(call["method"] == "operation.get" for call in peer.calls)

    asyncio.run(scenario())


def test_own_account_and_admin_operations_use_the_unmodified_public_payload(tmp_path):
    async def scenario():
        policy, peer = (
            AccessPolicy(config(tmp_path)),
            Peer({"own": {"method": "execution.run", "profile": "nemo"}}),
        )
        await policy.execute("alice", Request("operation.cancel", {"operation_id": "own"}), peer)
        assert peer.calls[-1] == {"method": "operation.cancel", "params": {"operation_id": "own"}}
        request = Request("daemon.status", {"method": "bad", "timeout": 0})
        await policy.execute("admin", request, peer)
        assert peer.calls[-1] == request.to_dict()
        with pytest.raises(AccessDenied):
            await policy.execute("unknown", request, peer)

    asyncio.run(scenario())
