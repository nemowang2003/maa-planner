"""Extract offline navigation candidates; never connect to a game or publish resources."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from maa_planner.daemon.tools.activity_assets import fetch, prepare_exporter
from maa_planner.daemon.tools.activity_navigation import discover_navigation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--dump", type=Path)
    inputs.add_argument("--bundle", type=Path)
    parser.add_argument(
        "--cache-directory", type=Path, help="Pinned exporter cache, required with --bundle"
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--activity-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出已存在；使用新的候选文件")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    if args.bundle:
        if args.cache_directory is None:
            parser.error("--bundle 需要 --cache-directory")
        dotnet = shutil.which("dotnet")
        if dotnet is None:
            parser.error("需要 .NET 8 dotnet；可通过 nix shell 提供")
        if not args.bundle.is_file() or args.bundle.stat().st_size > 64 * 1024 * 1024:
            parser.error("bundle 缺失或超过 64 MiB")
        exporter, _ = prepare_exporter(
            args.cache_directory / "activity-assets",
            "windows" if os.name == "nt" else "linux",
            fetch,
            threading.Event(),
        )
        with tempfile.TemporaryDirectory(prefix="maa-ui-discovery-") as folder:
            folder = Path(folder)
            log = folder / "export.log"
            with log.open("wb") as stream:
                process = subprocess.run(
                    [
                        dotnet,
                        str(exporter),
                        str(args.bundle.resolve()),
                        "-m",
                        "dump",
                        "--load-all",
                        "-g",
                        "containerFull",
                        "--export-asset-list",
                        "xml",
                        "-o",
                        str(folder / "dump"),
                    ],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "DOTNET_ROLL_FORWARD": "Major"},
                    timeout=120,
                )
            if process.returncode:
                raise ValueError("prefab 导出失败: " + log.read_text(errors="replace")[-2000:])
            if (
                sum(p.stat().st_size for p in (folder / "dump").rglob("*") if p.is_file())
                > 64 * 1024 * 1024
            ):
                raise ValueError("prefab 导出超过 64 MiB")
            value = discover_navigation(folder / "dump", catalog, args.activity_id)
        value["bundle_sha256"] = hashlib.sha256(args.bundle.read_bytes()).hexdigest()
    else:
        value = discover_navigation(args.dump, catalog, args.activity_id)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "status": value["status"],
                "buttons": len(value["buttons"]),
                "anchors": len(value["page_anchor_sprites"]),
                "rejected": len(value["rejected"]),
            }
        )
    )


if __name__ == "__main__":
    main()
