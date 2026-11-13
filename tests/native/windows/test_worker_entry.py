import asyncio
import sys

from maa_planner.daemon.worker import WorkerClient
from tests.helpers.execution import request


def test_worker_entry_keeps_native_stdout_out_of_protocol(tmp_path, monkeypatch):
    create = asyncio.create_subprocess_exec
    script = """import os, sys
import maa_planner.daemon.worker as worker
from maa_planner.daemon.execution.context import RunResult
def fake_run(request, *, control, **kwargs):
    os.write(1, b'native output is diagnostic\\n')
    import subprocess
    subprocess.run([sys._base_executable, '-c', 'print("inherited stdout is diagnostic")'], check=True)
    assert control.ready()
    return RunResult(request.attempt_id, 'succeeded')
worker.run_core = fake_run
worker.main()
"""

    async def create_fake(*args, **kwargs):
        return await create(sys.executable, "-c", script, *args[3:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_fake)

    async def scenario():
        client = WorkerClient(request(tmp_path), tmp_path / "state")

        async def admit():
            return True

        async def phase(value):
            pass

        result = await asyncio.wait_for(client.run(admit, phase), 10)
        assert result.status == "succeeded", result
        log = tmp_path / "state/workers/attempt-nemo/worker.log"
        assert b"native output is diagnostic" in log.read_bytes()
        assert b"inherited stdout is diagnostic" in log.read_bytes()

    asyncio.run(scenario())
