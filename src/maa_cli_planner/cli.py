from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import build_example_config, default_config_file, load_settings
from .expert import build_plan
from .maa import render_tasks, write_plan, write_profile
from .skland_client import collect_player_context, summarize_context_text


def main() -> None:
    parser = argparse.ArgumentParser(prog="maa_cli_planner")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_file(),
        help="planner 配置文件路径",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="写入示例配置")
    init_parser.add_argument("--force", action="store_true", help="覆盖已存在的配置")

    plan_parser = subparsers.add_parser("plan", help="拉取 skland 数据并尝试生成 MAA 任务配置")
    plan_parser.add_argument(
        "--dry-run", action="store_true", help="打印采集结果和规划结果，不写入文件"
    )

    args = parser.parse_args()

    if args.command == "init":
        run_init(args.config, force=args.force)
        return

    if args.command == "plan":
        asyncio.run(run_plan(args.config, dry_run=args.dry_run))
        return

    raise SystemExit(f"未知命令: {args.command}")


def run_init(config_file: Path, *, force: bool) -> None:
    path = config_file.expanduser().resolve()
    if path.exists() and not force:
        raise SystemExit(f"配置文件已存在: {path}，如需覆盖请添加 --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_example_config(), encoding="utf-8")
    print(path)


async def run_plan(config_file: Path, *, dry_run: bool) -> None:
    settings = load_settings(config_file)
    enabled_users = [user for user in settings.users if user.enabled]
    if not enabled_users:
        raise SystemExit("没有启用的用户")

    contexts = await asyncio.gather(
        *(collect_player_context(settings, user) for user in enabled_users)
    )
    plans = [build_plan(settings, user, context) for user, context in zip(enabled_users, contexts)]

    if dry_run:
        for context, plan in zip(contexts, plans):
            print(f"# {context.user_name} ({context.character_name})")
            print("# collected")
            print(summarize_context_text(context))
            print(f"# planner: {plan.reason}")
            if plan.tasks:
                print(render_tasks(plan).rstrip())
            else:
                print("# no tasks")
            print()
        return

    wrote_any = False
    for plan in plans:
        if not plan.tasks:
            print(f"{plan.user_name}: {plan.reason}")
            continue
        path = write_plan(settings, plan)
        print(f"{plan.user_name}: {path}")
        wrote_any = True

    if not wrote_any:
        print("未写入任何任务文件。")
        return

    if settings.write_profile and settings.profile_sections:
        path = write_profile(settings)
        print(f"profile: {path}")
