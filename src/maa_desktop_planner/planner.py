"""Extension point for task decisions.

A hook is a Python function ``build_patch(profile, gui_config) -> dict``. It
returns a recursive JSON patch merged into the profile's temporary config.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from .config import Profile

GuiPatch = dict[str, Any]
PlanHook = Callable[[Profile, dict[str, Any]], GuiPatch]


def build_patch(profile: Profile, gui_config: dict[str, Any]) -> GuiPatch:
    """Default decision layer. Add task rules here later."""
    del profile, gui_config
    return {}


def resolve_hook(spec: str | None) -> PlanHook:
    if not spec:
        return build_patch
    module_name, separator, function_name = spec.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError("hook 格式必须是 module:function")
    function = getattr(importlib.import_module(module_name), function_name)
    if not callable(function):
        raise TypeError(f"hook 不可调用: {spec}")
    return function
