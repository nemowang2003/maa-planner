"""Verify the actual file DACL used before publishing endpoint credentials."""

import ctypes
from ctypes import wintypes as w

from maa_planner.daemon.adapters.security import write_private


def test_endpoint_dacl_only_grants_the_current_user(tmp_path):
    path = tmp_path / "control.json"
    write_private(path, b'{"token":"test"}')
    load = getattr(ctypes, "WinDLL")
    advapi = load("advapi32", use_last_error=True)
    kernel = load("kernel32", use_last_error=True)
    get = advapi.GetNamedSecurityInfoW
    get.argtypes = [
        w.LPCWSTR,
        ctypes.c_int,
        w.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get.restype = w.DWORD
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_void_p,
        w.DWORD,
        w.DWORD,
        ctypes.POINTER(w.LPWSTR),
        ctypes.c_void_p,
    ]
    convert.restype = w.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor, text = ctypes.c_void_p(), w.LPWSTR()
    try:
        assert get(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor)) == 0
        assert convert(descriptor, 1, 4, ctypes.byref(text), None)
        assert text.value is not None
        assert text.value.startswith("D:P(A;")
        assert text.value.count("(") == 1
        assert ";;;S-1-5-21-" in text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)


def test_endpoint_rejects_actual_junction_and_junction_ancestor(tmp_path):
    import pytest

    from tests.helpers.filesystem import windows_junction

    for nested in (False, True):
        target = tmp_path / f"target-{nested}"
        target.mkdir()
        link = tmp_path / f"link-{nested}"
        with windows_junction(link, target):
            directory = link / "private" if nested else link
            with pytest.raises(PermissionError, match="重解析|junction"):
                write_private(directory / "control.json", b"private token")
            assert not list(target.iterdir())
