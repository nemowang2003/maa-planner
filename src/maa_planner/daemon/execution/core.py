"""Worker-local ctypes binding for the official MaaCore C API."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

from maa_planner.daemon.execution.annihilation import AnnihilationResult
from maa_planner.daemon.execution.annihilation_tasks import delegation_task
from maa_planner.daemon.execution.copilot_results import CopilotResults
from maa_planner.daemon.execution.event_shop import EventShopResult
from maa_planner.daemon.execution.tasks import normalize_fight
from maa_planner.daemon.planning.annihilation import is_annihilation_stage
from maa_planner.daemon.tools.storage import write_json

CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_void_p)


class CoreLibrary:
    def __init__(self, path: Path):
        self.dll_directory = os.add_dll_directory(str(path.parent)) if os.name == "nt" else None
        self.lib = ctypes.CDLL(str(path))
        p, s, i, b = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_uint8
        signatures = {
            "AsstGetVersion": (s, []),
            "AsstSetUserDir": (b, [s]),
            "AsstLoadResource": (b, [s]),
            "AsstSetStaticOption": (b, [i, s]),
            "AsstCreateEx": (p, [CALLBACK, p]),
            "AsstDestroy": (None, [p]),
            "AsstSetConnectionExtras": (None, [s, s]),
            "AsstSetInstanceOption": (b, [p, i, s]),
            "AsstConnect": (b, [p, s, s, s]),
            "AsstAppendTask": (i, [p, s, s]),
            "AsstStart": (b, [p]),
            "AsstStop": (b, [p]),
            "AsstRunning": (b, [p]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = result, args

    def version(self):
        return self.lib.AsstGetVersion().decode("utf-8").removeprefix("v")


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class GameSessionDisconnected(RuntimeError):
    pass


class CallbackResults:
    """Preserve raw events separately; summarize only explicit protocol evidence."""

    def __init__(self):
        self.tasks: dict[int, dict] = {}
        self.shops: dict[int, dict] = {}
        self.annihilation: dict[int, AnnihilationResult] = {}
        self.copilot = CopilotResults()
        self.rewards: dict[int, dict] = {}
        self.nodes: dict[int, list[tuple[str, str]]] = {}
        self.matches: dict[int, list[tuple[str, dict]]] = {}
        self.extras: dict[int, list[tuple[str, dict]]] = {}
        self.game_readiness: dict[int, dict] = {}
        self.logins: dict[int, dict] = {}
        self.session_failure: dict | None = None
        self.connection_error = False
        self.enhanced = False
        self.error = False

    def consume(
        self, message: int, payload: dict, reference: str, observed_at: float | None = None
    ):
        self.copilot.consume(
            message, payload, reference, observed_at if observed_at is not None else time.time()
        )
        if message in (0, 1, 10000):
            self.error = True
        if message in (0, 1):
            self.connection_error = True
        if message == 2 and payload.get("what") == "FastestWayToScreencap":
            self.enhanced = payload.get("details", {}).get("method") == "MumuExtras"
        task_id = payload.get("taskid")
        if type(task_id) is not int:
            return
        if (
            message == 20001
            and payload.get("subtask") == "ProcessTask"
            and self.tasks.get(task_id, {}).get("type") == "StartUp"
        ):
            details = payload.get("details")
            node = details.get("task") if isinstance(details, dict) else None
            if isinstance(node, str) and node.rsplit("@", 1)[-1] in {
                "GameStart",
                "StartToWakeUp",
                "StartToWakeUpOCR",
                "StartLoginBServer",
            }:
                self.logins[task_id] = {
                    "started_at": observed_at if observed_at is not None else time.time(),
                    "evidence": [reference],
                }
        if (
            message == 20001
            and payload.get("subtask") == "ProcessTask"
            and payload.get("taskchain") != "StartUp"
            and task_id in self.tasks
            and self.tasks[task_id]["type"] != "StartUp"
            and isinstance(payload.get("details"), dict)
        ):
            node = payload["details"].get("task")
            node = node.rsplit("@", 1)[-1] if isinstance(node, str) else ""
            if (
                node in {"OfflineConfirm", "OfflineConfirmAfterBattle"}
                and self.session_failure is None
            ):
                self.session_failure = {
                    "kind": "game_disconnected",
                    "task_id": task_id,
                    "node": node,
                    "evidence": reference,
                    "observed_at": observed_at if observed_at is not None else time.time(),
                }
                self.error = True
                self.tasks[task_id].update(
                    status="error", error_evidence=reference, error_kind="game_disconnected"
                )
        if (
            message == 20003
            and isinstance(payload.get("what"), str)
            and isinstance(payload.get("details"), dict)
        ):
            self.extras.setdefault(task_id, []).append((payload["what"], payload["details"]))
        if message == 20002 and payload.get("subtask") == "ProcessTask":
            node = (
                payload.get("details", {}).get("task")
                if isinstance(payload.get("details"), dict)
                else None
            )
            if isinstance(node, str):
                if self.tasks.get(task_id, {}).get("type") == "StartUp":
                    leaf = node.rsplit("@", 1)[-1]
                    timestamp = observed_at if observed_at is not None else time.time()
                    if leaf in {
                        "StartUp",
                        "StartAtHome",
                        "Home",
                        "ToggleSettingsMenu",
                        "StartWithSanity",
                    }:
                        login = self.logins.get(task_id)
                        if login is not None and "observed_at" not in login:
                            login["observed_at"] = timestamp
                            login["evidence"].append(reference)
                if (check := self.game_readiness.get(task_id)) is not None and node == check[
                    "node"
                ]:
                    check["evidence"].append(reference)
                result = payload["details"].get("result")
                if isinstance(result, dict):
                    self.matches.setdefault(task_id, []).append((node, result))
                self.nodes.setdefault(task_id, []).append((node.rsplit("@", 1)[-1], reference))
        if (
            task_id in self.rewards
            and message == 20002
            and payload.get("taskchain") == "Custom"
            and payload.get("subtask") == "ProcessTask"
        ):
            details = payload.get("details", {})
            node = details.get("task") if isinstance(details, dict) else None
            node = node.rsplit("@", 1)[-1] if isinstance(node, str) else ""
            state = self.rewards[task_id]
            if node == "MissionReady":
                state["page_anchor_verified"] = True
                state["evidence"].append(reference)
            if node in {"MissionControls", "CachedControls"} and state.get("page_anchor_verified"):
                state["page_verified"] = True
                state["evidence"].append(reference)
            if node in {"MissionControls", "CachedControls", "Claim", "CachedClaim"} and state.get(
                "page_verified"
            ):
                from maa_planner.daemon.tools.reward_roi import roi_from_match

                learned = roi_from_match(details.get("result"))
                if learned is not None:
                    state["claim_roi"] = learned
            if (
                node == "NoClaim"
                and state.get("page_anchor_verified")
                and not state.get("claim_attempted")
            ):
                state.update(
                    outcome="nothing_to_claim",
                    reason="claim_button_absent",
                    page_verified=True,
                    observed_at=observed_at or time.time(),
                )
                state["evidence"].append(reference)
            if node in {"Claim", "CachedClaim"} and state["page_verified"]:
                state["claim_attempted"] = True
            if (
                (state["page_verified"] or (node == "Locked" and state.get("page_anchor_verified")))
                and state["outcome"] != "claimed"
                and node in {"Received", "Locked", "Incomplete"}
                and (node != "Received" or state.get("claim_attempted"))
            ):
                state.update(
                    outcome={
                        "Received": "claimed",
                        "Locked": "unavailable",
                        "Incomplete": "incomplete",
                    }[node],
                    reason=node,
                    observed_at=observed_at or time.time(),
                )
                state["evidence"].append(reference)
        if task_id in self.tasks:
            state = {10001: "running", 10002: "completed", 10000: "error", 10004: "stopped"}.get(
                message
            )
            if state and not (self.session_failure and self.session_failure["task_id"] == task_id):
                self.tasks[task_id]["status"] = state
            if message == 10000 and not (
                self.session_failure and self.session_failure["task_id"] == task_id
            ):
                self.tasks[task_id]["error_evidence"] = reference
            if message == 20000:
                self.tasks[task_id]["subtask_errors"] += 1
        if (
            task_id in self.annihilation
            and self.tasks.get(task_id, {}).get("type") == "Custom"
            and payload.get("taskchain") == "Custom"
            and message == 20002
            and payload.get("subtask") == "ProcessTask"
        ):
            details = payload.get("details", {})
            name = details.get("task", "") if isinstance(details, dict) else ""
            # AbstractTask::callback strips everything through the last @.
            # Associate the leaf with the registered delegation task instead
            # of relying on a private prefix surviving the Core callback.
            if isinstance(name, str):
                node = name.rsplit("@", 1)[-1]
                reasons = {
                    "NoProxy": ("copilot_required", "no_auto_deploy"),
                    "OrdinaryProxy": ("skipped", "delegation_unavailable"),
                    "Unavailable": ("skipped", "delegation_unavailable"),
                    "InsufficientSanity": ("skipped", "insufficient_sanity"),
                    "Limit": ("incomplete", "attempt_limit"),
                    "Uncertain": ("unknown", "recognition_failed"),
                }
                if node in reasons:
                    outcome, reason = reasons[node]
                    self.annihilation[task_id] = replace(
                        self.annihilation[task_id],
                        outcome=outcome,
                        reason=reason,
                        observed_at=observed_at if observed_at is not None else time.time(),
                        evidence=(reference,),
                    )
        if (
            task_id in self.annihilation
            and payload.get("taskchain") == "Fight"
            and self.tasks.get(task_id, {}).get("type", "Fight") == "Fight"
        ):
            previous = self.annihilation[task_id]
            details = payload.get("details", {})
            details = details if isinstance(details, dict) else {}
            quota = details.get("annihilation_weekly_process")
            if (
                message == 20003
                and payload.get("what") == "StageDrops"
                and isinstance(quota, list)
                and len(quota) == 2
                and all(type(v) is int for v in quota)
                and 0 <= quota[0] <= quota[1]
                and quota[1] > 0
            ):
                self.annihilation[task_id] = replace(
                    previous,
                    outcome="completed" if quota[0] == quota[1] else "incomplete",
                    reason="weekly_quota",
                    current=quota[0],
                    total=quota[1],
                    observed_at=observed_at if observed_at is not None else time.time(),
                    evidence=(reference,),
                )
            elif (
                message in {20001, 20002}
                and isinstance(details.get("task"), str)
                and details["task"].rsplit("@", 1)[-1] == "UnableToAgent2"
                and previous.current is None
            ):
                self.annihilation[task_id] = replace(
                    previous,
                    outcome="copilot_required",
                    reason="no_auto_deploy",
                    observed_at=observed_at if observed_at is not None else time.time(),
                    evidence=(reference,),
                )
        if payload.get("taskchain") != "Custom":
            return
        details = payload.get("details", {})
        details = details if isinstance(details, dict) else {}
        first = payload.get("first", [])
        first = first if isinstance(first, list) else []
        context = payload.get("subtask") == "EventShopTask" or any(
            isinstance(name, str) and name.startswith("SS@Store@")
            for name in [details.get("task"), payload.get("pre_task"), *first]
        )
        if message == 10001:
            self.shops.pop(task_id, None)
        if context:
            shop = self.shops.setdefault(
                task_id,
                {"reason": "missing_evidence", "done": False, "started": False, "evidence": []},
            )
            node = str(details.get("task", "")).rsplit("@", 1)[-1]
            if message == 20001 and (payload.get("subtask") == "EventShopTask" or node == "Begin"):
                shop["started"] = True
            if message == 20002 and node in {"Underfunded", "UnderfundedOCR", "CheckUnlimited"}:
                shop["reason"] = (
                    "unlimited_commodity" if node == "CheckUnlimited" else "insufficient_currency"
                )
                shop["evidence"].append(reference)
        if task_id in self.shops:
            if message == 10002:
                self.shops[task_id]["done"] = True
            elif message in {10000, 10004, 20000}:
                self.shops[task_id]["done"] = False

    def shop_results(self):
        results = []
        for task_id, shop in self.shops.items():
            reason = shop["reason"]
            outcome = (
                "insufficient_currency"
                if reason == "insufficient_currency"
                else (
                    "completed"
                    if reason == "unlimited_commodity" and shop["done"] and shop["started"]
                    else "unknown"
                )
            )
            results.append(
                asdict(EventShopResult(task_id, outcome, reason, tuple(shop["evidence"][-4:])))
            )
        return results


def run(
    spec: dict,
    *,
    library_factory=CoreLibrary,
    stop: threading.Event | None = None,
    on_clearance=None,
    on_shop=None,
    shop_permission=None,
    on_login=None,
    orundum_start=None,
    on_orundum=None,
) -> int:
    stop = stop or threading.Event()
    root = Path(spec["directory"])
    root.mkdir(parents=True, exist_ok=True)
    events: queue.SimpleQueue = queue.SimpleQueue()
    results = CallbackResults()
    callback_failed = threading.Event()
    handled_tasks: set[int] = set()
    clearance_reports: list[dict] = []
    shop_reports: list[dict] = []
    processing_reports = []
    orundum_reports = []
    production_evidence = {}
    processing_errors = {}
    business_ids = spec.get("business_ids", [f"task:{i}" for i in range(len(spec["tasks"]))])
    businesses = {key: {"id": key, "status": "pending"} for key in business_ids}
    businesses.update({f["id"]: dict(f) for f in spec.get("business_failures", [])})
    active_index = None

    def business(index, status, error=None):
        item = businesses[business_ids[index]]
        item["status"] = status
        if error is not None:
            item["error"] = error

    def task_business(index, ids):
        checks = [results.game_readiness[i] for i in ids if i in results.game_readiness]
        if checks and not all(c["evidence"] for c in checks):
            results.error = True
            business(index, "error", {"reason": "game_resources_not_ready"})
            return
        if checks:
            businesses[business_ids[index]]["evidence"] = [
                ref for c in checks for ref in c["evidence"]
            ]
        if index in processing_errors:
            business(index, "error", processing_errors[index])
            return
        unconfirmed = next(
            (
                production_evidence[i]
                for i in ids
                if i in production_evidence and not production_evidence[i].reported
            ),
            None,
        )
        if unconfirmed is not None:
            results.error = True
            business(
                index,
                "error",
                {
                    "kind": "orundum_production",
                    "reason": "production_layout_unconfirmed",
                    "task_id": unconfirmed.task_id,
                    "evidence": [value[1] for value in unconfirmed.observed.values()],
                },
            )
            return
        failed = next(
            (results.tasks[i] for i in ids if results.tasks[i]["status"] != "completed"), None
        )
        if failed is None:
            business(index, "completed" if ids else "skipped")
            for identifier in ids:
                login = results.logins.get(identifier)
                if login is not None and "observed_at" in login:
                    businesses[business_ids[index]]["login_success"] = dict(login)
                    if on_login is not None:
                        on_login(dict(login))
        else:
            business(
                index,
                "error" if failed["status"] in {"error", "failed"} else "unknown",
                {
                    "status": failed["status"],
                    "task_id": failed["task_id"],
                    "evidence": failed.get("error_evidence"),
                },
            )

    def callback(message, raw, _):
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Core callback is not an object")
            events.put((message, payload, time.time()))
        except Exception:
            callback_failed.set()

    retained_callback = CALLBACK(callback)
    lib = None
    handle = None
    event_file = (root / "callbacks.jsonl").open("w", encoding="utf-8")
    line = 0

    def drain():
        nonlocal line
        while not events.empty():
            message, payload, observed_at = events.get()
            line += 1
            event_file.write(
                json.dumps(
                    {"message": message, "payload": payload, "received_at": observed_at},
                    ensure_ascii=False,
                )
                + "\n"
            )
            results.consume(message, payload, f"callbacks.jsonl:{line}", observed_at)
            for reducer in production_evidence.values():
                report = reducer.consume(message, payload, f"callbacks.jsonl:{line}")
                if report is not None:
                    orundum_reports.append(report)
                    if on_orundum is not None:
                        on_orundum(report)
        event_file.flush()
        if callback_failed.is_set():
            raise RuntimeError("Core 回调解码失败")

    status, reason = "failed", ""
    try:
        library = library_factory(Path(spec["library"]))
        expected = spec.get("expected_core_version")
        if expected is not None and library.version() != expected:
            raise ValueError(f"配置要求 Core {expected}，实际为 {library.version()}")
        lib = library.lib
        if not lib.AsstSetUserDir(str(root).encode()):
            raise RuntimeError("Core 设置私有目录失败")
        options = spec.get("options", {})
        for key, value in options.get("static_options", {}).items():
            option = {"cpu_ocr": 1, "gpu_ocr": 2}.get(key)
            if option is None or not lib.AsstSetStaticOption(option, str(value).encode()):
                raise ValueError(f"不支持或无法应用的静态选项: {key}")
        resources = list(spec["resources"])
        delegation = {}
        private_nodes = {}
        for index, task in enumerate(spec["tasks"]):
            if task["type"] == "Fight" and is_annihilation_stage(
                task.get("params", {}).get("stage")
            ):
                rescue_context = spec.get("annihilation_copilot")
                current = (
                    rescue_context["candidates"][0]
                    if rescue_context is not None
                    and task.get("params", {}).get("stage") == "Annihilation"
                    else None
                )
                converted, nodes = delegation_task(task, index, current=current)
                delegation[index] = converted
                private_nodes.update(nodes)
        if private_nodes:
            private = root / "annihilation-resources"
            (private / "resource").mkdir(parents=True, exist_ok=True)
            write_json(private / "resource/tasks.json", private_nodes)
            resources.append(str(private))
        for path in resources:
            if not lib.AsstLoadResource(str(path).encode()):
                raise RuntimeError(f"Core 加载资源失败: {path}")
        handle = lib.AsstCreateEx(retained_callback, None)
        if not handle:
            raise RuntimeError("Core 创建实例失败")
        for key, value in {
            "touch_mode": spec["touch_mode"],
            "kill_adb_on_exit": False,
            "client_type": spec["client_type"],
            **options.get("instance_options", {}),
        }.items():
            option = {
                "touch_mode": 2,
                "deployment_with_pause": 3,
                "adb_lite_enabled": 4,
                "kill_adb_on_exit": 5,
                "client_type": 6,
            }.get(key)
            if option is None or (key == "kill_adb_on_exit" and value):
                raise ValueError(f"不支持的实例选项: {key}")
            text = str(int(value)) if type(value) is bool else str(value)
            if key == "touch_mode":
                text = text.lower() if text in {"MiniTouch", "MaaTouch", "Adb"} else text
            if not lib.AsstSetInstanceOption(handle, option, text.encode()):
                raise RuntimeError(f"Core 实例选项失败: {key}")
        lib.AsstSetConnectionExtras(
            b"MuMuEmulator12",
            encoded(
                {
                    "path": spec["mumu_path"],
                    "index": spec["instance_index"],
                    "touch": options.get("instance_options", {}).get(
                        "touch_mode", spec["touch_mode"]
                    )
                    == "MumuExtras",
                    "client_type": spec["client_type"],
                }
            ),
        )
        if stop.is_set():
            raise InterruptedError("连接前取消")
        if not lib.AsstConnect(
            handle, spec["adb"].encode(), spec["address"].encode(), b"MuMuEmulator12"
        ):
            raise RuntimeError("Core 连接 MuMu 失败")
        # Callback delivery is asynchronous; wait for the completed connection's
        # screenshot-method evidence before submitting any game task.
        until = time.monotonic() + 5
        while not results.enhanced and time.monotonic() < until and not stop.is_set():
            drain()
            time.sleep(0.01)
        if stop.is_set():
            raise InterruptedError("连接期间取消")
        if not results.enhanced:
            raise RuntimeError("MuMu 增强截图未生效，拒绝回退普通截图并执行任务")
        if results.error:
            raise RuntimeError("Core 连接阶段报告错误，未提交任务")

        def check_session():
            if results.session_failure is not None:
                if stop.is_set():
                    raise InterruptedError("执行已取消")
                assert lib is not None
                lib.AsstStop(handle)
                raise GameSessionDisconnected("游戏掉线，会话失效；停止本轮后续任务")

        def execute_tasks(tasks, *, handled=False, plan_index=None):
            assert lib is not None
            check_session()
            manual = spec.get("infrast_orundum")
            applying = (
                manual is not None
                and plan_index is not None
                and business_ids[plan_index] == manual["business_id"]
            )
            if applying:
                # Processing belongs to this business, but cannot start the entry intent.
                if plan_index in processing_errors:
                    return []
                if stop.is_set():
                    raise InterruptedError("搓玉基建提交前取消")
                query = {k: manual[k] for k in ("intent_id", "business_id")}
                if orundum_start is None or not orundum_start(query):
                    raise RuntimeError("搓玉应用意图未获 daemon 持久确认，未提交基建")
            previous_error = results.error
            identifiers = []
            for offset, task in enumerate(tasks):
                task = normalize_fight(task)
                params = dict(task.get("params", {}))
                readiness = params.pop("game_resource_ready", None)
                reward = params.pop("activity_reward", None)
                if (
                    reward is not None
                    and not reward["available_from"] <= time.time() < reward["available_until"]
                ):
                    continue
                identifier = lib.AsstAppendTask(handle, task["type"].encode(), encoded(params))
                if reward is not None and identifier > 0:
                    results.rewards[identifier] = {
                        "task_id": identifier,
                        "activity_id": reward["activity_id"],
                        "page_source": reward["page_source"],
                        "claim_roi_key": reward["claim_roi_key"],
                        "outcome": "unknown",
                        "reason": "missing_evidence",
                        "page_verified": False,
                        "observed_at": None,
                        "evidence": [],
                    }

                if identifier <= 0:
                    raise RuntimeError(f"Core 拒绝任务 {task['type']}")
                if readiness is not None:
                    results.game_readiness[identifier] = {**readiness, "evidence": []}
                if applying:
                    from maa_planner.daemon.execution.orundum import ProductionEvidence

                    production_evidence[identifier] = ProductionEvidence(identifier, manual)
                identifiers.append(identifier)
                results.tasks[identifier] = {
                    "task_id": identifier,
                    "name": task.get("name", task["type"]),
                    "type": task["type"],
                    "status": "pending",
                    "subtask_errors": 0,
                    **({"plan_index": plan_index + offset} if plan_index is not None else {}),
                }
                stage = task.get("_annihilation_stage", task.get("params", {}).get("stage"))
                if task["type"] == "Copilot":
                    results.copilot.register(identifier, task.get("params", {}))
                if is_annihilation_stage(stage):
                    results.annihilation[identifier] = AnnihilationResult(identifier, stage)
            if not identifiers:
                return []
            if handled:
                handled_tasks.update(identifiers)
            if stop.is_set():
                raise InterruptedError("启动前取消")
            if not lib.AsstStart(handle):
                raise RuntimeError("Core 启动任务失败")
            while lib.AsstRunning(handle):
                drain()
                check_session()
                if not results.enhanced:
                    raise RuntimeError("增强截图在运行期间失效，停止执行")
                if stop.wait(0.05):
                    lib.AsstStop(handle)
                    raise InterruptedError("执行已取消")
            drain()
            check_session()
            until = time.monotonic() + 5
            while any(
                results.tasks[i]["status"] == "pending" or results.tasks[i]["status"] == "running"
                for i in identifiers
            ):
                if stop.is_set():
                    raise InterruptedError("等待 Core 终态期间取消")
                if time.monotonic() >= until:
                    raise RuntimeError("Core 未发送任务终态")
                time.sleep(0.01)
                drain()
                check_session()
            if not results.enhanced:
                raise RuntimeError("增强截图在运行期间失效，停止执行")
            if results.connection_error:
                raise RuntimeError("Core 运行期间连接异常")
            for identifier in identifiers:
                observation = results.annihilation.get(identifier)
                if observation is not None and observation.observed_at is None:
                    results.annihilation[identifier] = replace(
                        observation,
                        observed_at=time.time(),
                    )
            if handled:
                results.error = previous_error
            return identifiers

        processing_phases = {p["before_task"]: p for p in spec.get("infrast_processing", [])}

        def process_before(index):
            phase = processing_phases.get(index)
            if phase is None:
                return
            from maa_planner.daemon.execution.processing import execute_processing

            report = execute_processing(phase, execute_tasks, results, stop, root)
            processing_reports.append({"before_task": index, **report})
            if report["outcome"] == "error":
                results.error = True
                processing_errors[index] = {"kind": "processing", "reason": report["reason"]}

        clearance = spec.get("activity_copilot")
        shops = spec.get("activity_shops", [])
        shop_completed = {p["activity_id"]: {tuple(v) for v in p["completed"]} for p in shops}
        shop_navigation_status = {
            p["activity_id"]: p.get("navigation_status", "untried") for p in shops
        }
        purchased = set()
        if clearance is None and not shops and not delegation:
            for index, task in enumerate(spec["tasks"]):
                active_index = index
                business(index, "running")
                process_before(index)
                ids = execute_tasks([task], plan_index=index)
                task_business(index, ids)
                if (
                    task.get("params", {}).get("game_resource_ready")
                    and businesses[business_ids[index]]["status"] != "completed"
                ):
                    break
                if task["type"] == "StartUp" and any(
                    results.tasks[i]["status"] != "completed" for i in ids
                ):
                    break
        else:
            from maa_planner.daemon.execution.activity_copilot import farming_gate, run_clearance
            from maa_planner.daemon.execution.clearance_core import CoreClearanceDriver
            from maa_planner.daemon.planning.copilot import CopilotTarget

            allow_farming = True
            farming_error = None
            for index, task in enumerate(spec["tasks"]):
                active_index = index
                business(index, "running")
                if clearance is not None and index == clearance["before_task"]:
                    targets = tuple(
                        CopilotTarget(
                            **{
                                key: e[key]
                                for key in (
                                    "script_id",
                                    "stage_id",
                                    "code",
                                    "is_raid",
                                    "game_stage_id",
                                )
                            }
                        )
                        for e in clearance["targets"]
                    )
                    report_index = len(clearance_reports)
                    partial = []
                    clearance_reports.append(
                        {
                            "activity_id": clearance["activity_id"],
                            "outcome": "running",
                            "results": partial,
                            "warning": None,
                        }
                    )

                    def certified(item):
                        partial.append(asdict(item))
                        if item.outcome == "completed":
                            shop_completed.setdefault(clearance["activity_id"], set()).add(
                                (item.target.stage_id, item.target.is_raid)
                            )
                        if on_clearance is not None:
                            on_clearance(
                                {
                                    "activity_id": clearance["activity_id"],
                                    "outcome": "running",
                                    "results": [asdict(item)],
                                    "warning": None,
                                }
                            )

                    completed = set(tuple(v) for v in clearance["completed"])
                    report = run_clearance(
                        targets,
                        frozenset((v[0], v[1]) for v in clearance["completed"]),
                        CoreClearanceDriver(clearance, execute_tasks, results, stop),
                        on_result=certified,
                    )
                    completed.update(
                        (r.target.stage_id, r.target.is_raid)
                        for r in report.results
                        if r.outcome == "completed" and r.evidence
                    )
                    gate = farming_gate(clearance, completed)
                    selection = tuple(clearance.get("selection", ()))
                    warning = report.warning
                    if warning is None and any(v["status"] == "no_usable_set" for v in selection):
                        warning = {"reason": "no_usable_set", "selection": list(selection)}
                        if report.outcome != "cancelled":
                            report = replace(report, outcome="stopped")
                    report = replace(
                        report, farming_gate=gate, selection=selection, warning=warning
                    )
                    farming_error = (warning or {}).get("core_error")
                    allow_farming = gate["allowed"] and farming_error is None
                    clearance_reports[report_index] = {
                        "activity_id": clearance["activity_id"],
                        **asdict(report),
                    }
                    if on_clearance is not None:
                        on_clearance(clearance_reports[-1])
                    if report.outcome == "cancelled":
                        raise InterruptedError("自动通关阶段取消")
                if (
                    not allow_farming
                    and task["type"] == "Fight"
                    and not is_annihilation_stage(task.get("params", {}).get("stage"))
                ):
                    business(index, "error" if farming_error else "skipped", farming_error)
                    continue
                process_before(index)
                ids = execute_tasks(
                    [delegation.get(index, task)], handled=index in delegation, plan_index=index
                )
                rescue_context = spec.get("annihilation_copilot")
                if index in delegation and rescue_context is not None:
                    from maa_planner.daemon.execution.annihilation_rescue import run_rescue

                    for identifier in ids:
                        observation = results.annihilation.get(identifier)
                        if (
                            observation is None
                            or observation.outcome != "copilot_required"
                            or results.tasks[identifier]["status"] != "completed"
                        ):
                            continue
                        report = run_rescue(
                            rescue_context,
                            observation.stage,
                            execute_tasks,
                            results,
                            stop,
                            directory=root,
                        )
                        results.annihilation[identifier] = replace(
                            observation,
                            outcome="rescued"
                            if report["outcome"] == "verified"
                            else observation.outcome,
                            reason="copilot_" + report["reason"],
                            rescue=report,
                            evidence=(*observation.evidence, *report["evidence"]),
                        )
                        if report["outcome"] == "verified":
                            results.tasks[identifier].pop("plan_index", None)
                            ids = execute_tasks([delegation[index]], handled=True, plan_index=index)
                        break
                task_business(index, ids)
                if (
                    task.get("params", {}).get("game_resource_ready")
                    and businesses[business_ids[index]]["status"] != "completed"
                ):
                    break
                if any(results.tasks[i]["status"] != "completed" for i in ids):
                    if task["type"] == "StartUp":
                        break
                    continue
                for shop in shops:
                    if shop["after_task"] != index or shop["fingerprint"] in purchased:
                        continue
                    from maa_planner.daemon.execution.activity_shop import run_shop

                    if any(results.tasks[i]["status"] != "completed" for i in ids):
                        report = {
                            "activity_id": shop["activity_id"],
                            "fingerprint": shop["fingerprint"],
                            "after_task": shop.get("report_after_task", index),
                            "outcome": "unknown",
                            "reason": "fight_failed",
                            "evidence": [],
                            "observed_at": time.time(),
                        }
                    else:
                        report = run_shop(
                            {
                                **shop,
                                "navigation_status": shop_navigation_status[shop["activity_id"]],
                            },
                            shop_completed[shop["activity_id"]],
                            execute_tasks,
                            results,
                            stop,
                            shop_permission,
                        )
                    if report.get("navigation_status_before_attempt") == "succeeded":
                        shop_navigation_status[shop["activity_id"]] = "succeeded"
                    elif shop_navigation_status[shop["activity_id"]] == "untried":
                        navigation = report.get("navigation_outcome")
                        if navigation in {"succeeded", "failed"}:
                            shop_navigation_status[shop["activity_id"]] = (
                                "disabled" if navigation == "failed" else "succeeded"
                            )
                    if report.get("core_error"):
                        business(index, "error", report["core_error"])
                    elif (
                        report.get("outcome") == "unknown"
                        and businesses[business_ids[index]]["status"] != "error"
                    ):
                        business(index, "unknown")
                    shop_reports.append(report)
                    if on_shop is not None:
                        on_shop(report)
                    if report["outcome"] == "completed":
                        purchased.add(shop["fingerprint"])
        status = "finished"
    except InterruptedError as error:
        status, reason = "cancelled", str(error)
    except GameSessionDisconnected as error:
        status, reason = "failed", str(error)
        assert results.session_failure is not None
        if active_index is not None:
            business(active_index, "error", {"status": "error", **results.session_failure})
        for report in clearance_reports:
            if report["outcome"] == "running":
                report.update(
                    outcome="stopped",
                    warning={
                        "reason": "game_disconnected",
                        "core_error": {
                            "status": "error",
                            "task_id": results.session_failure["task_id"],
                            "evidence": results.session_failure["evidence"],
                        },
                    },
                )
    except Exception as error:
        reason = str(error)
        if active_index is not None and businesses[business_ids[active_index]]["status"] != "error":
            business(active_index, "unknown")
    finally:
        try:
            if handle and lib is not None:
                lib.AsstStop(handle)
                lib.AsstDestroy(handle)
        except Exception as error:
            status, reason = "recovery_required", f"Core 清理未完成: {error}"
        try:
            drain()
        except Exception as error:
            if status != "recovery_required":
                status, reason = "failed", str(error)
        finally:
            event_file.close()
        if status == "finished":
            status = (
                "failed"
                if results.error
                or any(b["status"] in {"error", "unknown"} for b in businesses.values())
                or any(
                    t["status"] in {"error", "failed"}
                    for t in results.tasks.values()
                    if "plan_index" in t
                )
                or any(
                    t["status"] != "completed"
                    for identifier, t in results.tasks.items()
                    if identifier not in handled_tasks
                )
                else "succeeded"
            )
            if any(
                r.get("warning", {}).get("core_error")
                for r in clearance_reports
                if r.get("warning")
            ):
                status = "failed"
                reason = reason or "自动抄作业收到 Core 执行错误，未确认通关"
        write_json(
            root / "core-result.json",
            {
                "status": status,
                "reason": reason,
                "session_failure": results.session_failure,
                "business_results": list(businesses.values()),
                "task_results": [
                    value
                    for identifier, value in results.tasks.items()
                    if identifier not in handled_tasks
                ],
                "handled_task_results": [
                    value
                    for identifier, value in results.tasks.items()
                    if identifier in handled_tasks
                ],
                "event_shop_results": results.shop_results(),
                "annihilation_results": [asdict(v) for v in results.annihilation.values()],
                "copilot_results": [
                    r for r in results.copilot.to_list() if r["task_id"] not in handled_tasks
                ],
                "activity_copilot_results": clearance_reports,
                "activity_shop_results": shop_reports,
                "infrast_processing_results": processing_reports,
                "orundum_results": orundum_reports,
                "activity_reward_results": list(results.rewards.values()),
                "task_checkpoints": [
                    {
                        "index": t["plan_index"],
                        "status": t["status"],
                        "task_id": t["task_id"],
                        "evidence": t.get("error_evidence"),
                    }
                    for t in results.tasks.values()
                    if "plan_index" in t
                ],
            },
        )
    return 0 if status == "succeeded" else 1
