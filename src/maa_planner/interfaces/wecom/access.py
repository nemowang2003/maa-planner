"""Account authorization at the interface boundary, without daemon imports."""

from maa_planner.schema import ADMIN_CHANNEL, METHODS, Request, Response

from .config import WeComConfig


class AccessDenied(ValueError):
    pass


async def invoke(client, request: Request):
    # Keep business params separate from Python call/timeout keyword arguments.
    return Response.from_dict(await client.request(request.to_dict())).unwrap()


class AccessPolicy:
    def __init__(self, config: WeComConfig):
        self.config = config

    def account(self, user: str, target) -> None:
        if (
            target == ADMIN_CHANNEL
            or not isinstance(target, str)
            or target not in self.config.profiles(user)
        ):
            raise AccessDenied("没有该账号的权限")

    async def execute(self, user: str, request: Request, client, *, dispatch=None):
        if user not in self.config.allowed_users:
            raise AccessDenied("用户未授权")
        method, params = request.method, request.params
        if user in self.config.admins:
            if dispatch is not None:
                dispatch()
            return await invoke(client, request)
        if method in {"operation.get", "operation.cancel", "operation.find"}:
            lookup = "operation.find" if method == "operation.find" else "operation.get"
            operation = await invoke(client, Request(lookup, params))
            if operation is None:
                return None
            await self.check_operation(user, operation, client)
            if method != "operation.cancel":
                return operation
        elif method == "execution.batch":
            METHODS[method].validate(params)
            jobs = params["jobs"]
            if not isinstance(jobs, list) or not 1 <= len(jobs) <= 100:
                raise ValueError("批次需要 1～100 个 jobs")
            for job in jobs:
                if not isinstance(job, dict) or job.get("method") not in {
                    "execution.run",
                    "copilot.run",
                }:
                    raise ValueError("批次成员仅接受 execution.run 或 copilot.run")
                self.account(user, job.get("profile"))
        elif method in METHODS and METHODS[method].profile != "none":
            METHODS[method].validate(params)
            self.account(user, params.get("profile"))
        else:
            raise AccessDenied("全局 API 仅管理员可用；可用 /alerts 查询自己的账号告警")
        if dispatch is not None:
            dispatch()
        return await invoke(client, request)

    async def check_operation(self, user: str, operation: dict, client) -> None:
        if operation.get("method") != "execution.batch":
            self.account(user, operation.get("profile"))
            return
        children = (operation.get("result") or {}).get("operations", [])
        if not isinstance(children, list) or not children or len(children) > 100:
            raise AccessDenied("无法确认批次账号范围")
        for identifier in children:
            child = await invoke(client, Request("operation.get", {"operation_id": identifier}))
            # Actual batches contain individual run/copilot operations, never nested batches.
            if child.get("method") not in {"execution.run", "copilot.run"}:
                raise AccessDenied("无法确认批次账号范围")
            self.account(user, child.get("profile"))
