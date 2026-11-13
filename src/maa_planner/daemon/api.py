"""RPC adaptation only. Business state and daemon queues are owned by services."""

from maa_planner.daemon.control_contract import OPERATION_ACTIONS, public_operation
from maa_planner.schema import METHODS, APIError, Request, Response


class ControlAPI:
    def __init__(self, *, operations, control, notifications):
        self.operations, self.control, self.notifications = operations, control, notifications

    async def handle(self, message, *, session_id=None):
        method = None
        try:
            request = Request.from_dict(message)
            method, params = request.method, request.params
            spec = METHODS.get(method)
            if spec is None:
                raise ValueError("未知方法")
            spec.validate(params)
            if method.startswith("daily."):
                from maa_planner.schema.daily import validate_daily

                validate_daily(method, params)
            if method == "api.describe":
                value = {
                    name: {
                        "kind": entry.kind,
                        "required": sorted(entry.required),
                        "optional": sorted(entry.optional),
                        "profile": entry.profile,
                    }
                    for name, entry in METHODS.items()
                }
            elif spec.kind == "operation":
                value = await self.accept(method, params)
                if session_id is not None:
                    identifiers = [value["operation_id"]]
                    if method == "execution.batch":
                        identifiers.extend(value["result"]["operations"])
                    for identifier in identifiers:
                        if self.notifications.claim(identifier, session_id):
                            self.operations.state_event("operation", {"operation_id": identifier})
                value = public_operation(value)
            elif method in {"operation.get", "operation.cancel", "operation.find"}:
                if method == "operation.find":
                    value = self.operations.find(params["idempotency_key"])
                else:
                    identifier = params["operation_id"]
                    if not isinstance(identifier, str) or not identifier:
                        raise ValueError("需要非空 operation_id")
                    value = (
                        self.operations.get(identifier)
                        if method == "operation.get"
                        else await self.operations.cancel(identifier)
                    )
                value = public_operation(value)
            else:
                value = await self.control.handle(method, params)
            return Response(result=value).to_dict()
        except APIError as error:
            return Response(
                error={
                    "code": error.code,
                    "message": str(error),
                    "details": {"method": method, **error.details},
                }
            ).to_dict()
        except Exception as error:
            return Response(
                error={
                    "code": "invalid_argument" if isinstance(error, ValueError) else "unavailable",
                    "message": str(error) or type(error).__name__,
                    "details": {"method": method},
                }
            ).to_dict()

    async def accept(self, method, params):
        key = params["idempotency_key"]
        if method == "infrast.orundum.request":
            return self.operations.accept_orundum(params)
        if method == "execution.batch":
            jobs = params["jobs"]
            if not isinstance(jobs, list):
                raise ValueError("jobs 必须为数组")
            commands = []
            for job in jobs:
                if not isinstance(job, dict) or job.get("method") not in {
                    "execution.run",
                    "copilot.run",
                }:
                    raise ValueError("批次成员只接受 execution.run 或 copilot.run")
                child = dict(job)
                child_method = child.pop("method")
                METHODS[child_method].validate({"idempotency_key": "member", **child})
                if "idempotency_key" in child:
                    raise ValueError("批次成员不接受 idempotency_key")
                commands.append(self.command(child_method, child))
            return await self.operations.accept_batch(commands, idempotency_key=key)
        return await self.operations.accept(self.command(method, params), idempotency_key=key)

    @staticmethod
    def command(method, params):
        return {
            "action": OPERATION_ACTIONS[method],
            "target": params.get("profile"),
            "expires_at": params.get("latest_start_at"),
            "arguments": {
                k: v
                for k, v in params.items()
                if k not in {"profile", "idempotency_key", "latest_start_at"}
            },
        }
