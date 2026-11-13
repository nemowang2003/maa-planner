"""Public method catalogue. No execution, daemon state or transport behavior."""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Method:
    kind: str
    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    profile: str = "none"

    def validate(self, params):
        if not isinstance(params, dict):
            raise ValueError("params 必须为对象")
        missing = self.required - params.keys()
        unknown = params.keys() - self.required - self.optional
        if missing or unknown:
            raise ValueError(f"参数字段不符：缺少 {sorted(missing)}，未知 {sorted(unknown)}")
        if "profile" in params and (
            not isinstance(params["profile"], str) or not params["profile"]
        ):
            raise ValueError("profile 必须为非空账号名称")


def operation(*required, optional=(), profile="none"):
    return Method(
        "operation",
        frozenset(
            {"idempotency_key", *required, *({"profile"} if profile == "required" else set())}
        ),
        frozenset(
            {"latest_start_at", *optional, *({"profile"} if profile == "optional" else set())}
        ),
        profile,
    )


def direct(*required, optional=(), profile="none", write=False):
    return Method(
        "command" if write else "query", frozenset(required), frozenset(optional), profile
    )


METHODS = MappingProxyType(
    {
        "api.describe": direct(),
        "daemon.status": direct(),
        "daemon.stop": direct(optional=("force",), write=True),
        "daemon.restart": direct(optional=("force",), write=True),
        "config.get": direct(),
        "config.replace": direct("toml", optional=("expected_revision",), write=True),
        "operation.get": direct("operation_id"),
        "operation.find": direct("idempotency_key"),
        "operation.cancel": direct("operation_id", write=True),
        "event.list": direct(optional=("after", "limit")),
        "alert.list": direct(optional=("profile", "active_only"), profile="optional"),
        "stage.confirm": direct(
            "profile",
            "activity_id",
            "stage_id",
            optional=("is_raid",),
            profile="required",
            write=True,
        ),
        "stage.unconfirm": direct(
            "profile",
            "activity_id",
            "stage_id",
            optional=("is_raid",),
            profile="required",
            write=True,
        ),
        "stage.progress.get": direct("profile", "activity_id", profile="required"),
        "daily.get": direct("profile", "occurrence_id", profile="required"),
        "daily.next": direct("profile", profile="required"),
        "daily.postpone": Method(
            "operation",
            frozenset(
                {
                    "profile",
                    "occurrence_id",
                    "expected_revision",
                    "expected_state_version",
                    "not_before",
                    "idempotency_key",
                }
            ),
            profile="required",
        ),
        "daily.skip": Method(
            "operation",
            frozenset(
                {
                    "profile",
                    "occurrence_id",
                    "expected_revision",
                    "expected_state_version",
                    "idempotency_key",
                }
            ),
            profile="required",
        ),
        "daily.run": Method(
            "operation",
            frozenset({"profile", "idempotency_key", "expires_at"}),
            frozenset({"not_before", "schedule_id", "inline"}),
            "required",
        ),
        "daily.retry": Method(
            "operation",
            frozenset(
                {
                    "profile",
                    "idempotency_key",
                    "occurrence_id",
                    "expected_revision",
                    "task_ids",
                    "expires_at",
                }
            ),
            frozenset({"not_before"}),
            "required",
        ),
        "daily.pause": Method(
            "operation", frozenset({"profile", "idempotency_key", "paused"}), profile="required"
        ),
        "daily.status": direct("profile", profile="required"),
        "execution.run": operation(
            "plan", optional=("options", "execution_timeout"), profile="required"
        ),
        "copilot.run": operation(
            "url",
            optional=(
                "formation",
                "ignore_requirements",
                "is_raid",
                "use_sanity_potion",
                "start_at",
                "select",
            ),
            profile="required",
        ),
        "execution.batch": Method("operation", frozenset({"idempotency_key", "jobs"})),
        "activity.fetch": operation(),
        "news.collect": operation("max_pages", "max_articles", "failure_backoff"),
        "assets.prepare": operation(
            "activity_id",
            "bundles",
            "resolve_dependencies",
            "max_bundle_bytes",
            "max_output_bytes",
            "export_timeout",
        ),
        "account.context.fetch": operation(profile="required"),
        "infrast.orundum.request": Method(
            "operation", frozenset({"profile", "idempotency_key"}), profile="required"
        ),
        "infrast.orundum.get": direct("profile", profile="required"),
        "schedule.plan.preview": operation("schedule_id", profile="required"),
        "emulator.list": operation(profile="optional"),
        "runtime.ensure": operation(),
        "resource.prepare_and_check": operation(),
        "resource.update": operation(),
        "game.inspect": operation(profile="optional"),
        "game.update": operation(profile="optional"),
        "runtime.prepare": operation(),
        "execution.recover": operation(profile="required"),
    }
)
