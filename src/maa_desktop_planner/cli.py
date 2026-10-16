"""Command line entry point for MAA Windows desktop planner."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config import default_config_file, example_config, load_settings
from .runner import ensure_stopped, restore_backups, run
from .updates import check_resource, check_software, plan_software_package, update_resource


def main() -> None:
    parser = argparse.ArgumentParser(prog="maa_desktop_planner")
    parser.add_argument("--config", type=Path, default=default_config_file())
    commands = parser.add_subparsers(dest="command")
    init = commands.add_parser("init", help="创建示例配置")
    init.add_argument("--force", action="store_true")
    commands.add_parser("run", help="检查更新、应用临时配置并行启动所有 MAA")
    commands.add_parser("restore", help="恢复异常中断后留下的 gui.new.json 备份")
    for name in ("check-software", "plan-software"):
        command = commands.add_parser(
            name, help="只读查询软件更新" if name == "check-software" else "定位完整包"
        )
        command.add_argument("--profile", help="只检查一个 profile 名称；默认检查全部")
        command.add_argument("--current-version", help="跨平台查询时手动指定本地版本")
    commands.add_parser("check-resource", help="只读检查所有资源目录")
    commands.add_parser("update-resource", help="更新所有资源目录，要求 MAA 已停止")
    args = parser.parse_args()

    if args.command == "init":
        path = args.config.expanduser()
        if path.exists() and not args.force:
            parser.error(f"配置文件已存在: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(example_config(), encoding="utf-8")
        print(path)
        return

    settings = load_settings(args.config)
    if args.command in (None, "run"):
        run(settings)
    elif args.command == "restore":
        restore_backups(settings)
    elif args.command in ("check-software", "plan-software"):
        ensure_stopped(settings.profiles)
        profiles = [
            profile
            for profile in settings.profiles
            if not args.profile or profile.name == args.profile
        ]
        if not profiles:
            parser.error(f"未知 profile: {args.profile}")
        for profile in profiles:
            status = check_software(profile.path, profile.channel, args.current_version)
            print(f"{profile.name}: {status.current} -> {status.latest}")
            if status.available and args.command == "plan-software":
                package = plan_software_package(
                    status.latest, profile.channel, profile.architecture
                )
                print(f"{package.name}: {package.url}")
    elif args.command in ("check-resource", "update-resource"):
        ensure_stopped(settings.profiles)
        if args.command == "update-resource":
            if os.name != "nt":
                raise OSError("更新 Windows MAA 资源需要在 Windows Python 中执行")
        resources = {profile.resource.resolve(): profile.resource for profile in settings.profiles}
        for resource in resources.values():
            status = (
                update_resource(resource)
                if args.command == "update-resource"
                else check_resource(resource)
            )
            print(f"{resource}: {status.current} -> {status.latest}")
