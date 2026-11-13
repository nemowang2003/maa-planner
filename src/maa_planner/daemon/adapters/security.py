"""Private endpoint discovery files on each native host."""

import os
import stat
from pathlib import Path


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise PermissionError("端点目录不能为符号链接")
    if os.name != "nt":
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("端点目录必须归当前用户所有且权限为 0700")


def private_file(path: Path) -> None:
    path.chmod(0o600)
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [
        w.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.c_void_p]
    token, sid, descriptor = w.HANDLE(), w.LPWSTR(), ctypes.c_void_p()

    def check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())

    try:
        check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        check(advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)))
        check(
            advapi.ConvertSidToStringSidW(
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.byref(sid)
            )
        )
        check(
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"D:P(A;;FA;;;{sid.value})", 1, ctypes.byref(descriptor), None
            )
        )
        check(advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor))
    finally:
        if token:
            kernel.CloseHandle(token)
        if sid:
            kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)


def write_private(path: Path, content: bytes) -> None:
    import tempfile

    from maa_planner.daemon.tools.storage import sync_directory

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    temporary = Path(name)
    try:
        # Apply the native ACL before any credential bytes reach the file.
        with os.fdopen(descriptor, "wb") as stream:
            private_file(temporary)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
