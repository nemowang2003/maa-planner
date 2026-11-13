import json
from dataclasses import replace
from pathlib import Path

import pytest

from maa_planner.daemon.execution.context import ImmediateControl, RunRequest
from maa_planner.daemon.execution.runner import run_core, runtime_adb
from maa_planner.daemon.execution.runtime import NAV_FILES, CoreRun, CoreRuntime
from maa_planner.daemon.execution.tasks import CoreTasks
from maa_planner.daemon.tools.locks import acquire, data_resource
from tests.helpers.execution import (
    CancelWhenStarted,
    FakeRuntime,
    file_contents,
    request,
)


@pytest.fixture
def core_request(tmp_path, monkeypatch):
    req = request(tmp_path)
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    (root / "bin/maa").touch()
    (root / "data/lib").mkdir(parents=True)
    (root / "data/lib/libMaaCore.so").touch()
    (root / "data/resource").mkdir()
    (root / "data/resource/version.json").write_text("{}")
    for name in NAV_FILES:
        file = root / "navigation" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('{"source":"frozen"}')
    spec = CoreRun(
        CoreRuntime(root / "bin/maa", root / "data", root / "navigation", tmp_path / "runs"),
        CoreTasks(
            json.dumps({"tasks": [{"type": "Fight", "params": {"stage": "1-7", "times": 1}}]})
        ),
    )
    monkeypatch.setattr(
        "maa_planner.daemon.execution.runtime.probe_runtime", lambda runtime: "versions OK"
    )
    monkeypatch.setattr(
        "maa_planner.daemon.execution.runner.runtime_adb", lambda request: tmp_path / "adb"
    )
    return replace(req, core=spec)


def execute_for(runtime, captures=None):
    def execute(spec, *, stop):
        runtime.started = True
        folder = Path(spec["directory"])
        if captures is not None:
            captures.append(spec)
        while runtime.hang and not stop.wait(0.01):
            pass
        if stop.is_set():
            runtime.stops += 1
        (folder / "core-result.json").write_text(
            json.dumps({"status": "succeeded", "task_results": [], "event_shop_results": []})
        )
        return 0

    return execute


def test_core_request_roundtrip_preserves_plan_and_attachments(core_request):
    assert RunRequest.from_dict(core_request.to_dict()) == core_request


def test_shop_metadata_and_private_navigation_survive_runner_startup_insertion(
    core_request, tmp_path
):
    from maa_planner.daemon.execution.runner import prepare
    from maa_planner.daemon.planning.activity_shop import shop_navigation
    from tests.helpers.activity_shop import shop_plan

    plan = shop_plan()
    tasks = CoreTasks(
        json.dumps(
            {
                "tasks": [{"type": "Fight", "params": {"stage": "1-7", "times": 30}}],
                "startup": True,
                "activity_shops": [plan],
                "resource_tasks": shop_navigation(plan, plan["navigation"]),
            }
        )
    )
    request = replace(core_request, core=replace(core_request.core, tasks=tasks))
    folder = tmp_path / "shop-private"
    folder.mkdir()
    document = prepare(request, folder, "127.0.0.1:5555")
    assert [t["type"] for t in document["tasks"]] == ["StartUp", "Fight"]
    assert document["activity_shops"][0]["after_task"] == 1
    assert document["activity_shops"][0]["report_after_task"] == 0
    assert (folder / "task-resources/resource/tasks.json").is_file()
    assert json.loads(tasks.document)["activity_shops"][0]["after_task"] == 0


def test_runner_preserves_explicit_adb_in_request(core_request, tmp_path):
    adb = tmp_path / "native-adb"
    adb.touch()
    spec = replace(core_request.core, runtime=replace(core_request.core.runtime, adb_path=adb))
    req = replace(core_request, core=spec)
    assert runtime_adb(req) == adb
    restored = RunRequest.from_dict(req.to_dict())
    assert restored.core is not None
    assert restored.core.runtime.adb_path == adb


def test_run_has_private_paths_and_does_not_touch_gui(core_request, tmp_path):
    req = core_request
    before = file_contents(req.binding.profile.gui_path / "config")
    runtime = FakeRuntime()
    captured = []

    result = run_core(
        req,
        runtime=runtime,
        execute=execute_for(runtime, captured),
        recovery_root=tmp_path / "recovery",
    )
    assert result.status == "succeeded", result
    assert result.game_result == "unknown"
    assert runtime.boots == runtime.shutdowns == 1
    profiles = captured
    folder = Path(captured[0]["directory"])
    assert profiles[0]["address"] == "127.0.0.1:12345"
    assert profiles[0]["instance_index"] == req.binding.instance_index
    assert not folder.exists()
    assert file_contents(req.binding.profile.gui_path / "config") == before


@pytest.mark.parametrize("vanishes", [False, True])
def test_copilot_requires_running_game_before_execution_and_rechecks_at_admission(
    core_request, tmp_path, vanishes
):
    spec = replace(core_request.core, tasks=CoreTasks('{"tasks":[{"type":"Copilot","params":{}}]}'))
    req = replace(core_request, core=spec)

    class Runtime(FakeRuntime):
        game_running = vanishes

        def require_game_running(self, binding):
            if not self.game_running:
                raise RuntimeError("明日方舟未运行")
            return self.inspect(binding)

    class Admission(ImmediateControl):
        def ready(self):
            runtime.game_running = False
            return True

    runtime = Runtime(running=True)
    result = run_core(
        req,
        runtime=runtime,
        control=Admission(),
        execute=execute_for(runtime),
        recovery_root=tmp_path / "recovery",
    )
    assert result.status == "failed"
    assert "未运行" in result.reason
    assert not runtime.started
    assert runtime.boots == runtime.shutdowns == 0


def test_running_copilot_never_boots_or_shuts_down_the_emulator(core_request, tmp_path):
    spec = replace(core_request.core, tasks=CoreTasks('{"tasks":[{"type":"Copilot","params":{}}]}'))
    req = replace(core_request, core=spec)

    class Runtime(FakeRuntime):
        def require_game_running(self, binding):
            return self.inspect(binding)

    runtime = Runtime(running=True)
    result = run_core(
        req, runtime=runtime, execute=execute_for(runtime), recovery_root=tmp_path / "recovery"
    )
    assert result.status == "succeeded"
    assert runtime.started
    assert runtime.boots == runtime.shutdowns == 0


def test_shared_runtime_update_blocks_before_boot(core_request, tmp_path):
    runtime = FakeRuntime()
    locks = tmp_path / "locks"
    with acquire([data_resource(core_request.core.runtime.data_dir)], root=locks):
        result = run_core(
            core_request, runtime=runtime, locks_root=locks, recovery_root=tmp_path / "recovery"
        )
    assert result.status == "blocked"
    assert runtime.boots == 0


def test_missing_navigation_never_boots_or_downloads(core_request, tmp_path):
    (core_request.core.runtime.navigation / NAV_FILES[0]).unlink()
    runtime = FakeRuntime()
    result = run_core(core_request, runtime=runtime, recovery_root=tmp_path / "recovery")
    assert result.status == "failed"
    assert "快照" in result.reason
    assert runtime.boots == 0


def test_denied_core_admission_leaves_gui_untouched(core_request, tmp_path):
    class Denied(ImmediateControl):
        def ready(self):
            return False

    runtime = FakeRuntime()
    result = run_core(
        core_request, runtime=runtime, control=Denied(), recovery_root=tmp_path / "recovery"
    )
    assert result.status == "cancelled"
    assert not runtime.started
    assert runtime.shutdowns == 0


def test_core_cancel_stops_its_process(core_request, tmp_path):
    runtime = FakeRuntime(hang=True)
    result = run_core(
        core_request,
        runtime=runtime,
        control=CancelWhenStarted(runtime),
        execute=execute_for(runtime),
        recovery_root=tmp_path / "recovery",
    )
    assert result.status == "cancelled"
    assert runtime.stops == 1
    assert runtime.shutdowns == 0


def test_core_stop_failure_fences_next_execution(core_request, tmp_path):
    class Runtime(FakeRuntime):
        def shutdown(self, binding):
            raise RuntimeError("模拟器关闭结果未知")

    runtime = Runtime()
    root = tmp_path / "recovery"
    result = run_core(
        core_request,
        runtime=runtime,
        execute=execute_for(runtime),
        recovery_root=root,
    )
    assert result.status == "recovery_required"
    assert runtime.shutdowns == 0
    next_req = replace(core_request, attempt_id="next")
    result = run_core(next_req, runtime=FakeRuntime(), recovery_root=root)
    assert result.status == "recovery_required"


@pytest.mark.parametrize("task_type", ["DepotMaintain", "UserDataUpdate", "WrongType"])
def test_unknown_gui_or_core_task_is_not_silently_discarded(task_type):
    with pytest.raises(ValueError, match="未知任务"):
        CoreTasks(json.dumps({"tasks": [{"type": task_type}]}))


def test_copilot_attachment_is_immutable_and_materialized(tmp_path):
    source = tmp_path / "input.json"
    original = {"stage_name": "main_01-07", "actions": [], "opers": []}
    source.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "type": "Copilot",
                        "params": {
                            "filename": "@file:copilot.json",
                            "formation": True,
                            "ignore_requirements": True,
                            "loop_times": 2,
                        },
                    }
                ],
                "attachments": {"copilot.json": original},
            }
        )
    )
    plan = CoreTasks.read(source)
    source.write_text("{}")
    folder = tmp_path / "private"
    folder.mkdir()
    task = plan.materialize(folder)["tasks"][0]
    assert task["type"] == "Copilot"
    assert task["params"]["formation"] is True
    assert task["params"]["ignore_requirements"] is True
    assert task["params"]["loop_times"] == 2
    assert json.loads(Path(task["params"]["filename"]).read_text()) == original


def test_missing_and_unsafe_attachment_rejected():
    with pytest.raises(ValueError, match="缺少附件"):
        CoreTasks('{"tasks":[{"type":"Copilot","params":{"filename":"@file:no.json"}}]}')
    with pytest.raises(ValueError, match="名称无效"):
        CoreTasks('{"tasks":[{"type":"Fight"}]}', '{"../secret.json":{}}')


def test_recovery_checks_process_before_clearing_fence(core_request, tmp_path, monkeypatch):
    from maa_planner.daemon.execution.runner import restore_core
    from maa_planner.daemon.tools.storage import RecoveryRequired

    class Runtime(FakeRuntime):
        def shutdown(self, binding):
            raise RuntimeError("模拟器关闭结果未知")

    runtime = Runtime()
    root = tmp_path / "recovery"
    run_core(
        core_request,
        runtime=runtime,
        execute=execute_for(runtime),
        recovery_root=root,
    )

    def busy(runtime):
        raise RecoveryRequired("CLI 尚在运行")

    monkeypatch.setattr("maa_planner.daemon.execution.runtime.ensure_core_stopped", busy)
    with pytest.raises(RecoveryRequired, match="尚在运行"):
        restore_core(core_request.binding, recovery_root=root, runtime=FakeRuntime())
    monkeypatch.setattr(
        "maa_planner.daemon.execution.runtime.ensure_core_stopped", lambda runtime: None
    )
    restore_core(core_request.binding, recovery_root=root, runtime=FakeRuntime())
    value = json.loads(next(root.rglob("core-execution.json")).read_text())
    assert value["phase"] == "finished"
    assert value["emulator_preserved"] is True


def test_declared_daily_tasks_are_all_passed_through():
    plan = CoreTasks.read(Path(__file__).resolve().parents[3] / "examples/tasks/daily.json")
    tasks = json.loads(plan.document)["tasks"]
    assert [t["type"] for t in tasks] == ["StartUp", "Recruit", "Infrast", "Fight", "Mall", "Award"]
    assert tasks[3]["params"]["medicine"] == tasks[3]["params"]["stone"] == 0


def test_execution_rejects_external_mutable_plan_files():
    with pytest.raises(ValueError, match="附件"):
        CoreTasks('{"tasks":[{"type":"Infrast","params":{"filename":"old-plan.json"}}]}')


def test_missing_core_plan_never_falls_back_to_gui(core_request):
    with pytest.raises(ValueError, match="完整 Core 任务计划"):
        replace(core_request, core=None)


def test_core_runs_without_any_gui_installation(core_request, tmp_path):
    from maa_planner.daemon.config import Profile

    req = replace(core_request, binding=replace(core_request.binding, profile=Profile("nemo")))
    runtime = FakeRuntime()
    result = run_core(
        req, runtime=runtime, execute=execute_for(runtime), recovery_root=tmp_path / "recovery"
    )
    assert result.status == "succeeded"
    assert req.binding.profile.gui_path is None
    assert RunRequest.from_dict(req.to_dict()) == req


def test_shared_resource_symlink_is_locked_by_target(core_request, tmp_path):
    resource = core_request.core.runtime.data_dir / "resource"
    shared = tmp_path / "shared-resource"
    resource.rename(shared)
    resource.symlink_to(shared, target_is_directory=True)
    runtime = FakeRuntime()
    with acquire([data_resource(shared)]):
        result = run_core(
            core_request,
            runtime=runtime,
            execute=execute_for(runtime),
            recovery_root=tmp_path / "recovery",
        )
    assert result.status == "blocked"
    assert runtime.started is False


def test_work_directory_cannot_be_inside_an_explicit_shared_root(core_request, tmp_path):
    for field in ("library_dir", "resource_dir"):
        with pytest.raises(ValueError, match="共享运行依赖"):
            replace(
                core_request.core.runtime,
                **{field: tmp_path / "shared"},
                work_dir=tmp_path / "shared/runs",
            )


def test_private_resource_nodes_load_after_hot_update_and_do_not_change_shared_files(
    core_request, tmp_path
):
    node = {"Planner@Test": {"algorithm": "JustReturn"}}
    from maa_planner.daemon.execution.png import encode_png
    from tests.helpers.png import PNG

    tasks = CoreTasks(
        json.dumps(
            {
                "tasks": [{"type": "Custom", "params": {"task_names": ["Planner@Test"]}}],
                "resource_tasks": node,
                "resource_images": {"Planner@Test.png": "@file:test.png"},
            }
        ),
        json.dumps({"test.png": encode_png(PNG)}),
    )
    req = replace(core_request, core=replace(core_request.core, tasks=tasks))
    before = file_contents(req.core.runtime.navigation)
    captured = []
    runtime = FakeRuntime()
    delegate = execute_for(runtime, captured)

    def execute(spec, *, stop):
        overlay = Path(spec["resources"][-1])
        assert overlay == Path(spec["directory"]) / "task-resources"
        assert json.loads((overlay / "resource/tasks.json").read_text()) == node
        assert (overlay / "resource/template/Planner@Test.png").read_bytes() == PNG
        assert spec["resources"][-2] == str(req.core.runtime.navigation)
        return delegate(spec, stop=stop)

    result = run_core(req, runtime=runtime, execute=execute, recovery_root=tmp_path / "recovery")
    assert result.status == "succeeded"
    assert file_contents(req.core.runtime.navigation) == before


@pytest.mark.parametrize(
    "nodes", [[], {"FightBegin": {}}, {"Planner@../bad": {}}, {"Planner@Test": []}]
)
def test_resource_nodes_require_project_namespace(nodes):
    with pytest.raises(ValueError, match="resource_tasks|私有任务资源"):
        CoreTasks(json.dumps({"tasks": [{"type": "Custom"}], "resource_tasks": nodes}))


@pytest.mark.parametrize("running", [False, True])
def test_automatic_run_never_starts_a_stopped_emulator(core_request, tmp_path, running):
    req = replace(core_request, options=replace(core_request.options, allow_emulator_start=False))
    assert RunRequest.from_dict(req.to_dict()) == req
    runtime = FakeRuntime(running=running)
    result = run_core(
        req, runtime=runtime, execute=execute_for(runtime), recovery_root=tmp_path / "recovery"
    )
    assert result.status == ("succeeded" if running else "blocked")
    assert runtime.started == running
    assert runtime.boots == int(running)
    assert runtime.shutdowns == 0
    if not running:
        assert "模拟器未启动" in result.reason
        assert not (tmp_path / "recovery").exists()


def test_runtime_selected_annihilation_script_is_archived(core_request, tmp_path):
    runtime = FakeRuntime()
    base_execute = execute_for(runtime)

    def execute(spec, *, stop):
        (Path(spec["directory"]) / "annihilation-rescue.json").write_text(
            '{"stage_name":"camp_r_34"}'
        )
        return base_execute(spec, stop=stop)

    result = run_core(
        core_request, runtime=runtime, execute=execute, recovery_root=tmp_path / "recovery"
    )
    assert result.status == "succeeded"
    assert result.log_directory is not None
    assert (
        json.loads((Path(result.log_directory) / "annihilation-rescue.json").read_text())[
            "stage_name"
        ]
        == "camp_r_34"
    )


@pytest.mark.parametrize("resume", [False, True])
def test_only_internal_recovery_admits_previous_foreground_game(
    core_request, tmp_path, monkeypatch, resume
):
    runtime = FakeRuntime(running=True)
    permissions = []

    def factory(**options):
        permissions.append(options["allow_foreground_game"])
        return runtime

    monkeypatch.setattr("maa_planner.daemon.execution.runner.EmulatorRuntime", factory)
    req = replace(core_request, options=replace(core_request.options, resume_game_session=resume))
    assert RunRequest.from_dict(req.to_dict()).options.resume_game_session is resume
    result = run_core(req, execute=execute_for(runtime), recovery_root=tmp_path / "recovery")
    assert result.status == "succeeded"
    assert permissions == [resume]


def test_runner_preserves_structured_session_failure(core_request, tmp_path):
    from dataclasses import asdict

    from maa_planner.daemon.execution.context import RunResult

    failure = {
        "kind": "game_disconnected",
        "task_id": 1,
        "node": "OfflineConfirm",
        "evidence": "callbacks.jsonl:7",
        "observed_at": 123,
    }

    def execute(spec, *, stop):
        (Path(spec["directory"]) / "core-result.json").write_text(
            json.dumps({"status": "failed", "reason": "游戏掉线", "session_failure": failure})
        )
        return 1

    result = run_core(
        core_request, runtime=FakeRuntime(), execute=execute, recovery_root=tmp_path / "recovery"
    )
    assert result.status == "failed" and result.reason == "游戏掉线"
    assert result.session_failure == failure
    assert RunResult.from_dict(asdict(result)) == result


def test_processing_metadata_templates_and_operator_file_reach_worker_after_startup(
    core_request, tmp_path
):
    from maa_planner.daemon.execution.runner import prepare
    from maa_planner.daemon.planning.processing import processing_nodes
    from tests.helpers.png import PNG

    recipe = {
        "item_id": "30034",
        "name": "聚酸酯块",
        "target": 20,
        "tier": "T3",
        "formula_id": "1",
        "costs": {"30033": 2, "4001": 300},
    }
    phase = {
        "before_task": 0,
        "navigation": "Planner@Processing0",
        "catalog_commit": "a" * 40,
        "stock": {},
        "recipes": [recipe],
        "operators": [
            {"name": "年", "filename": "@file:operator.json", "allowed_items": ["30034"]}
        ],
    }
    document = {
        "tasks": [{"type": "Infrast"}],
        "startup": True,
        "infrast_processing": [phase],
        "resource_tasks": processing_nodes(phase),
    }
    tasks = CoreTasks(
        json.dumps(document),
        json.dumps(
            {"operator.json": {"plans": [{"rooms": {"processing": [{"operators": ["年"]}]}}]}}
        ),
    )
    request = replace(core_request, core=replace(core_request.core, tasks=tasks))
    source = request.core.runtime.resources / "template/items/30034.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(PNG)
    folder = tmp_path / "processing-run"
    folder.mkdir()
    prepared = prepare(request, folder, "127.0.0.1:5555")
    assert prepared["infrast_processing"][0]["before_task"] == 1
    assert prepared["tasks"][1]["type"] == "Infrast"
    operator = prepared["infrast_processing"][0]["operators"][0]
    assert Path(operator["filename"]).is_file()
    template = folder / "task-resources/resource/template/Planner@ProcessingMaterial-30034.png"
    assert template.is_file() and source.read_bytes() == PNG
    nodes = json.loads((folder / "task-resources/resource/tasks.json").read_text())
    assert nodes["Planner@Processing0@Material0"]["template"] == template.name


@pytest.mark.parametrize("admitted", [False, True])
def test_resource_download_launches_game_only_under_worker_lock_after_admission(
    core_request, tmp_path, admitted
):
    from maa_planner.daemon.execution.context import ImmediateControl
    from maa_planner.daemon.execution.runner import run_core
    from maa_planner.daemon.planning.game_update import readiness_tasks

    sequence = []

    class Runtime(FakeRuntime):
        def ensure_stopped(self, binding):
            sequence.append("check")

        def start_game(self, binding):
            sequence.append("launch_game")

    class Control(ImmediateControl):
        def ready(self):
            sequence.append("admit")
            return admitted

    runtime = Runtime(running=True)
    req = replace(core_request, core=replace(core_request.core, tasks=readiness_tasks(30)))
    result = run_core(
        req,
        runtime=runtime,
        control=Control(),
        recovery_root=tmp_path / "recovery",
        locks_root=tmp_path / "locks",
        execute=execute_for(runtime),
    )
    assert ("launch_game" in sequence) == admitted
    if admitted:
        assert sequence.index("admit") < sequence.index("launch_game")
    else:
        assert result.status == "cancelled"
