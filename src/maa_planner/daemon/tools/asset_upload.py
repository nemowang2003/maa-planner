"""Build a minimal model-input package; never upload a whole decoded snapshot."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

from maa_planner.daemon.execution.png import MAX_PNG_BYTES, validate_png
from maa_planner.daemon.tools.storage import write_json


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    if path.is_symlink():
        raise ValueError("素材清单不能是符号链接")
    with path.open("rb") as stream:
        data = stream.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("素材／任务清单超过 16 MiB")
    return json.loads(data), digest(data)


def prepare_upload(snapshot, core_resources, requirements, *, core_version, max_bytes, output):
    if not isinstance(core_version, str) or not re.fullmatch(
        r"v?[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._-]*", core_version
    ):
        raise ValueError("需要固定 Core 版本")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024**3:
        raise ValueError("上传素材预算必须显式为 1..1 GiB")
    if not 1 <= len(core_resources) <= 16:
        raise ValueError("需要 1..16 个按加载顺序排列的完整 Core 资源目录")
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 100:
        raise ValueError("需要显式声明 1..100 项缺失任务及其必要素材")
    requested, seen = [], set()
    for item in requirements:
        if (
            not isinstance(item, dict)
            or set(item) != {"task", "assets"}
            or not isinstance(item["task"], str)
            or not re.fullmatch(r"[A-Za-z0-9_@-]{1,180}", item["task"])
            or item["task"] in seen
        ):
            raise ValueError("目标任务无效或重复")
        assets = item["assets"]
        if (
            not isinstance(assets, list)
            or not 1 <= len(assets) <= 1000
            or any(
                not isinstance(asset, str) or not 1 <= len(asset) <= 512 or "*" in asset
                for asset in assets
            )
            or len(set(assets)) != len(assets)
        ):
            raise ValueError("必须列出明确、非通配符的必要素材 ID")
        seen.add(item["task"])
        requested.append({"task": item["task"], "assets": list(assets)})
    tables, evidence, table_bytes = {}, [], 0
    for resource in core_resources:
        resource = Path(resource).resolve()
        task_folder = (resource / "tasks").resolve()
        legacy = resource / "tasks.json"
        if not legacy.is_file() and not (task_folder / "tasks.json").is_file():
            raise ValueError("需要完整 Core 资源根目录，不能只用单份任务表推断缺失")
        files = ([legacy] if legacy.is_file() else []) + sorted(task_folder.rglob("*.json"))
        if len(files) > 512:
            raise ValueError("Core 任务文件超过预算")
        for path in files:
            data, checksum = read_json(path)
            table_bytes += path.stat().st_size
            if table_bytes > 64 * 1024 * 1024:
                raise ValueError("Core 任务表总大小超过 64 MiB")
            if (
                not isinstance(data, dict)
                or not data
                or any(not isinstance(value, dict) for value in data.values())
            ):
                raise ValueError("Core 任务表无效或为空，不能猜测所有任务缺失")
            for name, node in data.items():
                tables[name] = {**tables.get(name, {}), **node}
            evidence.append({"order": len(evidence), "sha256": checksum})

    def algorithm(name):
        if name not in tables:
            return algorithm(name.split("@", 1)[1])
        node = tables[name]
        return node.get(
            "algorithm", algorithm(node["baseTask"]) if "baseTask" in node else "MatchTemplate"
        )

    def provided(name, visiting=frozenset()):
        if name in visiting:
            raise ValueError("Core 任务继承关系循环")
        if name not in tables:
            # Core resolves prefixed derived tasks against their unprefixed base.
            if "@" not in name:
                return False
            base = name.split("@", 1)[1]
            if not provided(base, visiting | {name}):
                return False
            # Implicit MatchTemplate derivatives can require a new PNG under
            # their own name; the generic base alone does not supply it.
            return algorithm(base) in {"OcrDetect", "JustReturn"}
        base = tables[name].get("baseTask")
        if base is not None and (
            not isinstance(base, str) or not provided(base, visiting | {name})
        ):
            raise ValueError("Core 任务表缺少继承基节点，不能当作完整能力快照")
        return True

    missing = [item for item in requested if not provided(item["task"])]
    skipped = [item["task"] for item in requested if item not in missing]
    if not missing:
        return {"status": "not_needed", "asset_count": 0, "skipped_tasks": skipped}
    snapshot = Path(snapshot)
    if snapshot.is_symlink():
        raise ValueError("导出快照不能是符号链接")
    snapshot = snapshot.resolve()
    manifest, manifest_sha = read_json(snapshot / "manifest.json")
    ready, _ = read_json(snapshot / "READY")
    source = manifest["source"]
    if ready.get("identity") != digest(json.dumps(source, sort_keys=True).encode()):
        raise ValueError("素材来源身份或 READY 无效")
    if any(
        not isinstance(source.get(key), str) or not source[key]
        for key in ("activity_id", "res_version", "export_key")
    ):
        raise ValueError("素材来源版本缺失")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or len(assets) > 100000:
        raise ValueError("导出素材清单无效")
    index = {}
    for asset in assets:
        if (
            not isinstance(asset, dict)
            or not isinstance(asset.get("id"), str)
            or asset["id"] in index
        ):
            raise ValueError("导出素材身份无效或重复")
        index[asset["id"]] = asset
    selected = sorted({asset for item in missing for asset in item["assets"]})
    if len(selected) > 1000:
        raise ValueError("必要素材超过 1000 项预算")
    blobs, packaged, total = {}, [], 0
    for identifier in selected:
        if identifier not in index:
            raise ValueError(f"缺失任务的必要素材不存在: {identifier}")
        asset = index[identifier]
        relative = Path(asset["path"])
        path = snapshot / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not path.resolve().is_relative_to(snapshot)
            or any(
                (snapshot.joinpath(*relative.parts[:i])).is_symlink()
                for i in range(1, len(relative.parts) + 1)
            )
        ):
            raise ValueError("必要素材路径逃逸或包含符号链接")
        with path.open("rb") as stream:
            content = stream.read(MAX_PNG_BYTES + 1)
        checksum = digest(content)
        if checksum != asset["sha256"] or len(content) != asset["size"]:
            raise ValueError("必要素材哈希／大小变化")
        validate_png(content)
        if checksum not in blobs:
            total += len(content)
            if total > max_bytes:
                raise ValueError("必要素材超过显式上传预算")
            blobs[checksum] = content
        packaged.append(
            {
                "id": identifier,
                "path": f"images/{checksum}.png",
                "sha256": checksum,
                "size": len(content),
            }
        )
    packet = {
        "status": "selected_inputs",
        "activity_id": source["activity_id"],
        "res_version": source["res_version"],
        "bundle_hash": source["export_key"],
        "core_version": core_version,
        "source_manifest_sha256": manifest_sha,
        "core_tables_sha256": digest(json.dumps(evidence, sort_keys=True).encode()),
        "core_table_count": len(evidence),
        "requirements": missing,
        "assets": packaged,
        "frames": [],
        "required_strata": [],
    }
    identity = digest(json.dumps(packet, sort_keys=True).encode())
    metadata_bytes = sum(
        len((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
        for value in (packet, {"identity": identity})
    )
    if total + metadata_bytes > max_bytes:
        raise ValueError("必要素材与清单超过显式上传预算")
    output = Path(output)
    output.mkdir()  # Claim a new directory; never overwrite an existing package.
    try:
        (output / "images").mkdir()
        for checksum, content in blobs.items():
            (output / "images" / (checksum + ".png")).write_bytes(content)
        write_json(output / "workspace.json", packet)
        write_json(output / "READY", {"identity": identity})
    except BaseException:
        shutil.rmtree(output)
        raise
    return {
        "status": "selected_inputs",
        "package": str(output),
        "identity": identity,
        "asset_count": len(packaged),
        "file_count": len(blobs),
        "bytes": total + metadata_bytes,
        "image_bytes": total,
        "missing_tasks": [item["task"] for item in missing],
        "skipped_tasks": skipped,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--core-resource", type=Path, action="append", required=True)
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    requirements, _ = read_json(args.requirements)
    print(
        json.dumps(
            prepare_upload(
                args.snapshot,
                args.core_resource,
                requirements,
                core_version=args.core_version,
                max_bytes=args.max_bytes,
                output=args.output,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
