"""Execute one immutable Core plan without touching a GUI configuration."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from maa_planner.daemon.adapters.windows import directory_link
from maa_planner.daemon.execution.annihilation import AnnihilationResult
from maa_planner.daemon.execution.context import (
    CancelledRun,
    EmulatorRuntime,
    ImmediateControl,
    RunRequest,
    RunResult,
)
from maa_planner.daemon.execution.copilot_results import CopilotResult
from maa_planner.daemon.execution.event_shop import EventShopResult
from maa_planner.daemon.execution.tasks import json_object
from maa_planner.daemon.tools.locks import (
    BusyError,
    acquire,
    data_resource,
    emulator_resource,
    maa_resource,
    state_directory,
)
from maa_planner.daemon.tools.storage import RecoveryRequired, write_json


def binding_resources(binding):
    result = [emulator_resource(binding.mumu_root, binding.instance_index)]
    if binding.profile.gui_path is not None:
        result.append(maa_resource(binding.profile.gui_path))
    return tuple(result)


def core_resources(request: RunRequest):
    assert request.core is not None
    runtime = request.core.runtime
    return (
        *binding_resources(request.binding),
        data_resource(runtime.data_dir, shared=True),
        data_resource(runtime.resources, shared=True),
        data_resource(runtime.library, shared=True),
        data_resource(runtime.navigation, shared=True),
    )


def check_fences(root: Path, keys: set[str]) -> None:
    for path in (
        [*root.rglob("core-execution.json"), *root.rglob("cli-execution.json")]
        if root.exists()
        else ()
    ):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("phase") != "finished" and keys.intersection(value["resources"]):
            raise RecoveryRequired(f"上次 Core 执行未完成，请核对进程及模拟器后恢复: {path}")


def prepare(request: RunRequest, folder: Path, address: str) -> dict:
    assert request.core is not None
    runtime, spec = request.core.runtime, request.core
    document = spec.tasks.materialize(folder)
    # Core's loader expects a directory containing resource/. Link only inside
    # the private attempt; never overwrite shared resource files.
    link = folder / "resource"
    if os.name == "nt":
        directory_link(link, runtime.resources)
    else:
        link.symlink_to(runtime.resources, target_is_directory=True)
    resources = [str(folder), str(runtime.navigation)]
    client = document.get("client_type", "Official")
    if client not in {"Official", "Bilibili"}:
        resources = [
            str(folder),
            str(runtime.resources / "global" / client),
            str(runtime.navigation),
            str(runtime.navigation / "resource/global" / client),
        ]
    overrides = document.pop("resource_tasks", {})
    images = document.pop("resource_images", {})
    if document.get("infrast_processing"):
        from maa_planner.daemon.execution.processing import material_templates

        images.update(
            material_templates(document["infrast_processing"], runtime.resources, folder / "files")
        )
    if overrides or images:
        private_resources = folder / "task-resources"
        (private_resources / "resource").mkdir(parents=True)
        write_json(private_resources / "resource/tasks.json", overrides)
        if images:
            templates = private_resources / "resource/template"
            templates.mkdir()
            for name, filename in images.items():
                (templates / name).write_bytes(Path(filename).read_bytes())
        resources.append(str(private_resources))
    tasks = document["tasks"]
    if document.get("startup", False):
        if "activity_copilot" in document:
            document["activity_copilot"]["before_task"] += 1
        for phase in document.get("infrast_processing", []):
            phase["before_task"] += 1
        for shop in document.get("activity_shops", []):
            shop["report_after_task"] = shop["after_task"]
            shop["after_task"] += 1
        if "business_ids" in document:
            document["business_ids"].insert(0, "lifecycle:startup")
        tasks.insert(
            0, {"type": "StartUp", "params": {"client_type": client, "start_game_enabled": True}}
        )
    if document.get("closedown", False):
        if "business_ids" in document:
            document["business_ids"].append("lifecycle:closedown")
        tasks.append({"type": "CloseDown", "params": {"client_type": client}})
    document = {
        "directory": str(folder),
        "library": str(runtime.core_path),
        "expected_core_version": runtime.expected_core_version,
        "resources": resources,
        "options": json_object(spec.profile_json),
        "mumu_path": str(request.binding.mumu_root),
        "instance_index": request.binding.instance_index,
        "adb": str(runtime_adb(request)),
        "address": address,
        "client_type": client,
        "touch_mode": runtime.touch_mode or "MiniTouch",
        "tasks": tasks,
        **(
            {"infrast_orundum": document["infrast_orundum"]}
            if "infrast_orundum" in document
            else {}
        ),
        **({"business_ids": document["business_ids"]} if "business_ids" in document else {}),
        **(
            {"business_failures": document["business_failures"]}
            if "business_failures" in document
            else {}
        ),
        **(
            {"activity_copilot": document["activity_copilot"]}
            if "activity_copilot" in document
            else {}
        ),
        **({"activity_shops": document["activity_shops"]} if "activity_shops" in document else {}),
        **(
            {"infrast_processing": document["infrast_processing"]}
            if "infrast_processing" in document
            else {}
        ),
        **(
            {"annihilation_copilot": document["annihilation_copilot"]}
            if "annihilation_copilot" in document
            else {}
        ),
    }
    write_json(folder / "core-spec.json", document)
    return document


def runtime_adb(request: RunRequest) -> Path:
    assert request.core is not None
    return request.core.runtime.adb_path or request.binding.mumu_root / "nx_main/adb.exe"


def run_core(
    request: RunRequest,
    *,
    runtime=None,
    control=None,
    recovery_root=None,
    locks_root=None,
    execute=None,
) -> RunResult:
    assert request.core is not None
    # Copilot and persisted daily recovery may resume an existing game session.
    # New ordinary requests still treat a manually active game as external ownership.
    tasks = json_object(request.core.tasks.document)["tasks"]
    copilot = all(task["type"] in {"Copilot", "SSSCopilot", "ParadoxCopilot"} for task in tasks)
    runtime = runtime or EmulatorRuntime(
        allow_emulator_start=request.options.allow_emulator_start,
        allow_foreground_game=copilot or request.options.resume_game_session,
        package_name=request.core.runtime.game_package if request.core else None,
    )
    control = control or ImmediateControl()
    recovery_root = recovery_root or state_directory() / "recovery"
    spec = request.core
    binding = request.binding
    result = RunResult(request.attempt_id, "failed")
    started_emulator = False
    record = None
    folder = None
    state = {}
    dependencies = core_resources(request)
    keys = {r.key for r in dependencies}
    try:
        with acquire(dependencies, root=locks_root):
            check_fences(recovery_root, {r.key for r in dependencies if not r.shared})
            runtime.ensure_stopped(binding)
            if copilot:
                runtime.require_game_running(binding)
            if not request.options.allow_emulator_start and not runtime.inspect(binding).running:
                raise BusyError("模拟器未启动；自动任务不会启动模拟器，请手动启动或使用显式 API")
            spec.runtime.validate()
            runtime.ensure_stopped(binding)
            folder = spec.runtime.work_dir / request.attempt_id
            folder.mkdir(parents=True, exist_ok=False, mode=0o700)
            record = recovery_root / "core" / request.attempt_id / "core-execution.json"
            record.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
            import psutil

            identity = {"pid": os.getpid(), "created": psutil.Process().create_time()}
            write_json(folder / "worker-process.json", identity)
            before = runtime.inspect(binding)
            state = {
                "phase": "preparing",
                "resources": sorted(keys),
                "request": request.to_dict(),
                "emulator_was_running": before.running,
                "directory": str(folder),
            }
            write_json(record, state)
            try:
                started_emulator = not copilot and not before.running
                ready = (
                    runtime.require_game_running(binding)
                    if copilot
                    else runtime.boot(binding, control, request.options.startup_timeout)
                )
                if not ready.address:
                    raise ValueError("模拟器 ADB 地址不可用")
                document = prepare(request, folder, ready.address)
                control.report("ready")
                if not control.ready() or control.cancelled():
                    raise CancelledRun("未获得启动许可")
                runtime.ensure_stopped(binding)
                if copilot:
                    runtime.require_game_running(binding)
                state["phase"] = "launching"
                write_json(record, state)
                control.report("launching")
                if any(t.get("params", {}).get("game_resource_ready") for t in document["tasks"]):
                    runtime.start_game(binding)
                state.update(phase="running", process=identity)
                write_json(record, state)
                control.report("running")
                from maa_planner.daemon.execution.core import run

                stop, done = threading.Event(), threading.Event()
                timed_out = threading.Event()
                deadline = time.monotonic() + request.options.execution_timeout

                def monitor():
                    while not done.wait(0.05):
                        if time.monotonic() >= deadline:
                            timed_out.set()
                            stop.set()
                        elif control.cancelled():
                            stop.set()

                watcher = threading.Thread(target=monitor, daemon=True)
                watcher.start()
                try:
                    code = (
                        execute(document, stop=stop)
                        if execute is not None
                        else run(
                            document,
                            stop=stop,
                            on_clearance=getattr(control, "clearance", None),
                            on_shop=getattr(control, "shop", None),
                            shop_permission=getattr(control, "shop_permission", None),
                            on_login=getattr(control, "login", None),
                            orundum_start=getattr(control, "orundum_start", None),
                            on_orundum=getattr(control, "orundum", None),
                        )
                    )
                finally:
                    done.set()
                    watcher.join()
                report = json_object((folder / "core-result.json").read_text(encoding="utf-8"))
                if report.get("status") == "recovery_required":
                    raise RecoveryRequired(report.get("reason", "Core 清理未完成"))
                if timed_out.is_set():
                    raise TimeoutError("Core 执行超时")
                if control.cancelled():
                    raise CancelledRun("运行被取消")
                result = RunResult(
                    request.attempt_id, "succeeded" if code == 0 else "failed", exit_code=code
                )
            except CancelledRun as error:
                result = RunResult(request.attempt_id, "cancelled", str(error))
            except Exception as error:
                result = RunResult(
                    request.attempt_id,
                    "recovery_required" if isinstance(error, RecoveryRequired) else "failed",
                    str(error),
                )
            finally:
                try:
                    with suppress(Exception):
                        control.report("finalizing")
                    if result.status == "recovery_required":
                        raise RecoveryRequired(result.reason)
                    # Cancellation (including a lost daemon pipe) leaves the game in place.
                    if (
                        started_emulator
                        and not control.cancelled()
                        and result.status != "cancelled"
                    ):
                        runtime.shutdown(binding)
                    state.update(phase="finished", result=result.status)
                    write_json(record, state)
                except Exception as error:
                    result = replace(result, status="recovery_required", reason=str(error))
                    state.update(phase="conflict", error=str(error))
                    write_json(record, state)
    except Exception as error:
        result = RunResult(
            request.attempt_id,
            "blocked"
            if isinstance(error, BusyError)
            else "recovery_required"
            if isinstance(error, RecoveryRequired)
            else "failed",
            str(error),
        )
    logs = folder
    if folder is not None and result.status == "recovery_required":
        try:
            # Cleanup uncertainty cannot erase already-certified room production.
            report = json_object((folder / "core-result.json").read_text(encoding="utf-8"))
            result = replace(result, orundum_results=tuple(report.get("orundum_results", ())))
        except (OSError, ValueError, TypeError):
            pass
    if folder is not None and result.status != "recovery_required":
        try:
            report = json_object((folder / "core-result.json").read_text(encoding="utf-8"))
            result = replace(
                result,
                event_shop_results=tuple(
                    EventShopResult.from_dict(item) for item in report.get("event_shop_results", ())
                ),
                task_results=tuple(report.get("task_results", ())),
                task_checkpoints=tuple(report.get("task_checkpoints", ())),
                annihilation_results=tuple(
                    AnnihilationResult.from_dict(v) for v in report.get("annihilation_results", ())
                ),
                copilot_results=tuple(
                    CopilotResult.from_dict(v) for v in report.get("copilot_results", ())
                ),
                activity_copilot_results=tuple(report.get("activity_copilot_results", ())),
                activity_shop_results=tuple(report.get("activity_shop_results", ())),
                activity_reward_results=tuple(report.get("activity_reward_results", ())),
                session_failure=report.get("session_failure"),
                business_results=tuple(report.get("business_results", ())),
                orundum_results=tuple(report.get("orundum_results", ())),
                reason=report.get("reason", result.reason),
            )
            if result.status == "succeeded" and report.get("status") != "succeeded":
                result = replace(result, status="failed")
        except (OSError, ValueError, TypeError):
            if result.status == "succeeded":
                result = replace(result, status="failed", reason="Core 缺少有效的结构化执行结果")
    if folder is not None and folder.exists() and result.status != "recovery_required":
        logs = recovery_root.parent / "logs" / request.attempt_id
        try:
            logs.mkdir(parents=True, exist_ok=True, mode=0o700)
            for source in list(folder.rglob("*.log*")) + [
                folder / "callbacks.jsonl",
                folder / "core-result.json",
                folder / "annihilation-rescue.json",
            ]:
                if source.is_file() and not source.is_symlink():
                    target = logs / source.relative_to(folder)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            shutil.rmtree(folder)
        except OSError:
            # Diagnostic cleanup is best effort; the task result remains authoritative.
            logs = folder
    return replace(result, log_directory=str(logs) if logs is not None else None)


def restore_core(binding, *, recovery_root=None, runtime=None) -> None:
    """Release crash fences after observing stopped processes; never kill an emulator."""
    from maa_planner.daemon.execution.runtime import ensure_core_stopped

    runtime = runtime or EmulatorRuntime()
    root = recovery_root or state_directory() / "recovery"
    keys = {r.key for r in binding_resources(binding) if not r.shared}
    for path in (
        [*root.rglob("core-execution.json"), *root.rglob("cli-execution.json")]
        if root.exists()
        else ()
    ):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("phase") == "finished" or not keys.intersection(value["resources"]):
            continue
        if path.name == "cli-execution.json":
            raise RecoveryRequired("旧 CLI 恢复记录需先人工核对并迁移，不能由 Core 恢复器自动清除")
        request = RunRequest.from_dict(value["request"])
        if request.core is None:
            raise RecoveryRequired("Core 恢复记录缺少运行依赖")
        with acquire(core_resources(request)):
            runtime.ensure_stopped(request.binding)
            runtime.inspect(request.binding)
            ensure_core_stopped(request.core.runtime)
            value.update(phase="finished", result="recovered", emulator_preserved=True)
            write_json(path, value)
            directory = Path(value["directory"])
            if directory == request.core.runtime.work_dir / request.attempt_id:
                shutil.rmtree(directory, ignore_errors=True)
