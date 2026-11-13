"""Native Windows utilities; no cross-system path or process bridge."""

from __future__ import annotations

import base64
import json
import ntpath
import os
import subprocess
from pathlib import Path


def require_windows_host() -> None:
    if os.name != "nt":
        raise OSError("仅支持原生 Windows MuMu，不支持 WSL 或 Linux 模拟器")


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def powershell(script: str, *, cwd: Path | None = None) -> str:
    # Encoding the script avoids two independent shells interpreting quotes/paths.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-EncodedCommand", encoded],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def local_path(value: str | Path) -> Path:
    return Path(value).expanduser()


def filesystem_path(path: Path) -> Path:
    """Use extended paths for file IO without changing system-wide policy."""
    if os.name != "nt":
        return path
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        value = "UNC\\" + value[2:]
    return Path("\\\\?\\" + value)


def windows_path(value: str | Path) -> str:
    return str(value)


def process_key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def maa_processes() -> list[dict]:
    script = """
$ErrorActionPreference = 'Stop'
$processes = @(Get-CimInstance Win32_Process -Filter "Name = 'MAA.exe' OR Name = 'MAA.Updater.exe'" | Select-Object ProcessId, Name, ExecutablePath)
ConvertTo-Json -InputObject $processes -Compress
"""
    result = json.loads(powershell(script))
    if not isinstance(result, list):
        raise ValueError("Windows 进程查询未返回数组")
    return result


def directory_link(link: Path, target: Path) -> None:
    """Create a Windows directory symlink without a shell or junction fallback."""
    powershell(f"""
$ErrorActionPreference='Stop'
if (-not ('NativeLinks' -as [type])) {{
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NativeLinks {{
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CreateSymbolicLink(string source, string dest, int flags);
}}
'@
}}
if (-not [NativeLinks]::CreateSymbolicLink({ps_quote(windows_path(link))}, {ps_quote(windows_path(target))}, 3)) {{
    throw ('Cannot create private data link: ' + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
}}
""")


def lock_handle(stream, *, shared: bool, unlock: bool = False) -> None:
    """Windows byte-range locks support concurrent readers across worker processes."""
    if os.name != "nt":
        raise OSError("Windows 文件锁仅在原生 Windows 可用")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel.UnlockFileEx if unlock else kernel.LockFileEx
    fn.restype = wintypes.BOOL
    fn.argtypes = (
        [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        if unlock
        else [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
    )
    handle = msvcrt.get_osfhandle(stream.fileno())
    overlap = Overlapped()
    ok = (
        fn(handle, 0, 1, 0, ctypes.byref(overlap))
        if unlock
        else fn(handle, 1 | (0 if shared else 2), 0, 1, 0, ctypes.byref(overlap))
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


class ProcessJob:
    """Own the actual worker and Core children; allow explicit emulator breakaway."""

    def __init__(self, pid: int):
        if os.name != "nt":
            raise OSError("Windows job requires native Windows")
        import ctypes
        from ctypes import wintypes as w

        import psutil

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", w.DWORD),
                ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t),
                ("active", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in ("read", "write", "other", "read_bytes", "write_bytes", "other_bytes")
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        for name, restype, args in [
            ("CreateJobObjectW", w.HANDLE, [ctypes.c_void_p, w.LPCWSTR]),
            ("SetInformationJobObject", w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]),
            ("OpenProcess", w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
            ("AssignProcessToJobObject", w.BOOL, [w.HANDLE, w.HANDLE]),
            ("IsProcessInJob", w.BOOL, [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]),
            ("CloseHandle", w.BOOL, [w.HANDLE]),
        ]:
            fn = getattr(k, name)
            fn.restype, fn.argtypes = restype, args
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = Limits()
            limits.basic.flags = 0x2000 | 0x0800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
            if not k.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = psutil.Process(pid)
            # The actual worker waits for ownership admission. Venv redirectors
            # are ancestors; assigning both levels can fail on Windows.
            for identifier in [pid, *(child.pid for child in process.children(recursive=True))]:
                handle = k.OpenProcess(0x0100 | 0x0001 | 0x1000, False, identifier)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    present = w.BOOL()
                    if not k.IsProcessInJob(handle, self.handle, ctypes.byref(present)):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if not present.value and not k.AssignProcessToJobObject(self.handle, handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    k.CloseHandle(handle)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            getattr(self, "kernel").CloseHandle(self.handle)
            self.handle = None
