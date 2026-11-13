"""Collect host-specific acceptance tests without platform skips in business tests."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent


def pytest_ignore_collect(collection_path: Path) -> bool:
    if collection_path.is_relative_to(ROOT / "windows"):
        return sys.platform != "win32"
    return False


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.is_relative_to(ROOT):
            item.add_marker(pytest.mark.native)
