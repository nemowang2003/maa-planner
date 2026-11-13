import asyncio
import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from maa_planner.daemon.worker import WorkerClient, WorkerControl
from tests.helpers.execution import request

SCRIPT = r"""
import json, socket, sys, os, psutil
stream = sys.stdin.buffer
request = json.load(open(sys.argv[1]))
sys.stdout.buffer.write((json.dumps({'type':'identity','pid':os.getpid(),
    'created':psutil.Process().create_time()})+'\n').encode())
sys.stdout.buffer.flush()
assert json.loads(stream.readline()) == {'type':'owned'}
sys.stdout.buffer.write(b'{"type":"ready"}\n')
sys.stdout.buffer.flush()
permission = json.loads(stream.readline())['allowed']
status = 'cancelled'
if permission:
    sys.stdout.buffer.write(b'{"type":"phase","phase":"running"}\n')
    sys.stdout.buffer.flush()
    if sys.argv[2] == 'cancel':
        assert json.loads(stream.readline())['type'] == 'cancel'
    else:
        status = 'succeeded'
result = {'attempt_id':request['attempt_id'], 'status':status}
if sys.argv[2] == 'shop':
    result['event_shop_results'] = [{'task_id':1, 'outcome':'insufficient_currency',
        'reason':'insufficient_currency', 'evidence':['state/debug/asst.log:10']}]
if sys.argv[2] == 'login':
    report = {'started_at':1,'observed_at':2,'evidence':['start','home']}
    sys.stdout.buffer.write((json.dumps({'type':'login_success','report':report})+'\n').encode())
    sys.stdout.buffer.flush()
if sys.argv[2] == 'activity-shop':
    report = {'activity_id':'event', 'outcome':'completed', 'reason':'unlimited_commodity'}
    sys.stdout.buffer.write((json.dumps({'type':'activity_shop','report':report})+'\n').encode())
    sys.stdout.buffer.flush()
    result['activity_shop_results'] = [report]
if sys.argv[2] == 'orundum':
    sys.stdout.buffer.write(b'{"type":"orundum_start","query_id":9,"query":{"intent_id":"intent","business_id":"task:0"}}\n')
    sys.stdout.buffer.flush()
    assert json.loads(stream.readline()) == {'type':'orundum_start','query_id':9,'allowed':True}
    report = {'intent_id':'intent','business_id':'task:0','mode':'orundum','outcome':'applied'}
    sys.stdout.buffer.write((json.dumps({'type':'orundum_applied','report':report})+'\n').encode())
    sys.stdout.buffer.flush()
    result['orundum_results'] = [report]
if sys.argv[2] == 'shop-permission':
    sys.stdout.buffer.write(b'{"type":"shop_permission","query_id":7,"query":{"activity_id":"event"}}\n')
    sys.stdout.buffer.flush()
    assert json.loads(stream.readline()) == {'type':'shop_permission','query_id':7,'status':'disabled'}
sys.stdout.buffer.write((json.dumps({'type':'result','result':result})+'\n').encode())
sys.stdout.buffer.flush()
stream.close()
"""


@pytest.mark.parametrize(
    "mode,allowed,expected",
    [
        ("done", True, "succeeded"),
        ("done", False, "cancelled"),
        ("cancel", True, "cancelled"),
        ("shop", True, "succeeded"),
        ("activity-shop", True, "succeeded"),
        ("login", True, "succeeded"),
        ("shop-permission", True, "succeeded"),
        ("orundum", True, "succeeded"),
    ],
)
def test_real_subprocess_private_admission_and_cancellation(
    tmp_path, monkeypatch, mode, allowed, expected
):
    create = asyncio.create_subprocess_exec

    async def create_fake(*args, **kwargs):
        path = args[args.index("--request") + 1]
        return await create(sys.executable, "-c", SCRIPT, path, mode, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_fake)

    async def scenario():
        client = WorkerClient(request(tmp_path), tmp_path / "state")
        running = asyncio.Event()
        shops = []
        logins = []

        async def login_report(report):
            logins.append(report)

        client.login_callback = login_report

        async def shop_report(report):
            shops.append(report)

        client.shop_callback = shop_report

        async def shop_permission(query):
            assert query == {"activity_id": "event"}
            return "disabled"

        client.shop_permission_callback = shop_permission
        orundum = []

        async def orundum_start(query):
            assert query == {"intent_id": "intent", "business_id": "task:0"}
            return True

        async def orundum_report(report):
            orundum.append(report)

        client.orundum_start_callback = orundum_start
        client.orundum_callback = orundum_report

        async def admit():
            return allowed

        async def phase(value):
            if value == "running":
                running.set()

        task = asyncio.create_task(client.run(admit, phase))
        if mode == "cancel":
            await asyncio.wait_for(running.wait(), 5)
            await client.cancel()
        result = await asyncio.wait_for(task, 5)
        assert result.status == expected
        if mode == "login":
            assert logins == [{"started_at": 1, "observed_at": 2, "evidence": ["start", "home"]}]
        if mode == "shop":
            assert result.event_shop_results[0].outcome == "insufficient_currency"
            assert result.event_shop_results[0].evidence == ("state/debug/asst.log:10",)
        if mode == "activity-shop":
            assert list(result.activity_shop_results) == shops
            assert shops[0]["reason"] == "unlimited_commodity"
        if mode == "orundum":
            assert list(result.orundum_results) == orundum
        assert client.process is not None
        assert client.process.returncode == 0
        assert not (tmp_path / "state/workers/attempt-nemo/request.json").exists()

    asyncio.run(scenario())


def test_control_parent_eof_denies_admission_and_cancels():
    parent, child = socket.socketpair()
    control = WorkerControl(child)
    with ThreadPoolExecutor() as executor:
        waiting = executor.submit(control.ready)
        assert json.loads(parent.recv(65536))["type"] == "ready"
        parent.close()
        assert waiting.result(timeout=2) is False
        assert control.cancelled()
    child.close()


@pytest.mark.parametrize("status", ["untried", "succeeded", "disabled", "pending"])
def test_control_shop_permission_round_trip_ignores_stale_replies(status):
    parent, child = socket.socketpair()
    control = WorkerControl(child)
    with ThreadPoolExecutor() as executor:
        waiting = executor.submit(control.shop_permission, {"activity_id": "event"})
        query = json.loads(parent.recv(65536))
        assert query["type"] == "shop_permission"
        parent.sendall(
            json.dumps({"type": "shop_permission", "query_id": 0, "status": "untried"}).encode()
            + b"\n"
        )
        parent.sendall(
            json.dumps(
                {"type": "shop_permission", "query_id": query["query_id"], "status": status}
            ).encode()
            + b"\n"
        )
        assert waiting.result(timeout=2) == status
    parent.close()
    child.close()


def test_worker_crash_without_result_requires_recovery(tmp_path, monkeypatch):
    create = asyncio.create_subprocess_exec

    async def create_fake(*args, **kwargs):
        path = args[args.index("--request") + 1]
        script = SCRIPT[: SCRIPT.index("permission =")] + "sys.exit(17)\n"
        return await create(sys.executable, "-c", script, path, "crash", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_fake)

    async def scenario():
        client = WorkerClient(request(tmp_path), tmp_path / "state")

        async def admit():
            return True

        async def phase(value):
            pass

        result = await asyncio.wait_for(client.run(admit, phase), 10)
        assert result.status == "recovery_required"
        assert "17" in result.reason

    asyncio.run(scenario())


def test_hung_worker_is_terminated_after_cancel_grace(tmp_path, monkeypatch):
    create = asyncio.create_subprocess_exec
    script = (
        SCRIPT[: SCRIPT.index("    if sys.argv[2] == 'cancel':")]
        + "    import time; time.sleep(60)\n"
    )

    async def create_fake(*args, **kwargs):
        path = args[args.index("--request") + 1]
        return await create(sys.executable, "-c", script, path, "hang", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_fake)
    monkeypatch.setattr("maa_planner.daemon.worker.STOP_GRACE", 0.1)

    async def scenario():
        client = WorkerClient(request(tmp_path), tmp_path / "state")
        running = asyncio.Event()

        async def admit():
            return True

        async def phase(value):
            running.set()

        task = asyncio.create_task(client.run(admit, phase))
        await asyncio.wait_for(running.wait(), 5)
        await client.cancel()
        result = await asyncio.wait_for(task, 5)
        assert result.status == "recovery_required"
        assert client.process is not None and client.process.returncode is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("allowed", [False, True])
def test_control_orundum_start_requires_matching_persisted_ack(allowed):
    parent, child = socket.socketpair()
    control = WorkerControl(child)
    with ThreadPoolExecutor() as executor:
        waiting = executor.submit(
            control.orundum_start, {"intent_id": "intent", "business_id": "task:0"}
        )
        query = json.loads(parent.recv(65536))
        assert query["type"] == "orundum_start"
        parent.sendall(
            json.dumps({"type": "orundum_start", "query_id": 0, "allowed": True}).encode() + b"\n"
        )
        parent.sendall(
            json.dumps(
                {"type": "orundum_start", "query_id": query["query_id"], "allowed": allowed}
            ).encode()
            + b"\n"
        )
        assert waiting.result(timeout=2) is allowed
    parent.close()
    child.close()


def test_control_eof_denies_orundum_submission():
    parent, child = socket.socketpair()
    control = WorkerControl(child)
    with ThreadPoolExecutor() as executor:
        waiting = executor.submit(
            control.orundum_start, {"intent_id": "intent", "business_id": "task:0"}
        )
        assert json.loads(parent.recv(65536))["type"] == "orundum_start"
        parent.close()
        assert waiting.result(timeout=2) is False
    child.close()
