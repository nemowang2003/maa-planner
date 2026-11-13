"""Local endpoint discovery and file I/O, independent of daemon implementation."""

import json
import os
from pathlib import Path

from maa_planner.schema import Endpoint


def default_endpoint() -> Path:
    from platformdirs import user_state_path

    if root := os.environ.get("XDG_STATE_HOME"):
        return Path(root) / "maa-planner" / "control.json"
    return user_state_path("maa-planner", appauthor=False) / "control.json"


def read_endpoint(path: Path) -> Endpoint:
    return Endpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))
