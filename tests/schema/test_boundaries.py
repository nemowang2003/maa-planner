import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from maa_planner.schema import METHODS, ConfigurationUpdate, Request, Response


def test_shared_schema_import_does_not_load_implementations():
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import sys,json; import maa_planner.schema; "
            "print(json.dumps([name for name in sys.modules if name.startswith('maa_planner')]))",
        ],
        text=True,
    )
    assert all(
        not name.startswith(("maa_planner.daemon", "maa_planner.interfaces"))
        for name in json.loads(output)
    )


def test_implementation_packages_only_share_schema():
    root = Path(__file__).resolve().parents[2] / "src/maa_planner"
    for folder, prohibited in (
        ("daemon", "maa_planner.interfaces"),
        ("interfaces", "maa_planner.daemon"),
        ("schema", "maa_planner.daemon"),
        ("schema", "maa_planner.interfaces"),
    ):
        for path in (root / folder).rglob("*.py"):
            package = ".".join(("maa_planner", *path.relative_to(root).parts[:-1]))
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        module = importlib.util.resolve_name("." * node.level + module, package)
                    targets = [module, *(module + "." + alias.name for alias in node.names)]
                    assert not any(
                        target == prohibited or target.startswith(prohibited + ".")
                        for target in targets
                    ), path
                if isinstance(node, ast.Import):
                    assert not any(alias.name.startswith(prohibited) for alias in node.names), path


def test_protocol_validates_envelopes_and_operation_arguments():
    request = Request("config.get")
    assert Request.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError):
        Request.from_dict({"method": 17})
    with pytest.raises(ValueError):
        Request.from_dict({"method": "config.get", "params": []})
    with pytest.raises(ValueError):
        Response.from_dict({"result": {}, "error": {"message": "ambiguous"}})
    with pytest.raises(RuntimeError, match="failure"):
        Response.from_dict(Response(error={"message": "failure"}).to_dict()).unwrap()
    with pytest.raises(ValueError):
        ConfigurationUpdate.from_dict({"toml": "content", "expected_revision": 1})
    METHODS["copilot.run"].validate(
        {"idempotency_key": "test", "profile": "nemo", "url": "prts://s56648", "select": [3, 1]}
    )
    with pytest.raises(ValueError, match="未知"):
        METHODS["resource.update"].validate({"idempotency_key": "test", "profile": "nemo"})
    assert "submit" not in METHODS and "batch.submit" not in METHODS


def test_public_client_does_not_import_daemon_or_interfaces():
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import sys,json; import maa_planner.client; print(json.dumps([name for name in sys.modules if name.startswith('maa_planner')]))",
        ],
        text=True,
    )
    assert not any(
        name.startswith(("maa_planner.daemon", "maa_planner.interfaces"))
        for name in json.loads(output)
    )
