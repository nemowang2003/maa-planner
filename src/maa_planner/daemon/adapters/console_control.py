"""Best-effort Windows console close/logoff/shutdown notification handling."""

from __future__ import annotations

import os
import threading

EVENTS = {2: "WINDOWS_CONSOLE_CLOSE", 5: "WINDOWS_LOGOFF", 6: "WINDOWS_SHUTDOWN"}


class ConsoleControl:
    def __init__(self, notify):
        self.notify = notify
        self.finished = threading.Event()
        self.callback = None
        self.kernel = None

    def handle(self, event):
        reason = EVENTS.get(event)
        if reason is None:
            return False
        self.notify(reason)
        # Windows may kill a console process as soon as its handler returns.
        # Its own shutdown deadline can be shorter than this best-effort wait.
        self.finished.wait(4)
        return True

    def __enter__(self):
        if os.name != "nt":
            raise OSError("Windows 控制通知仅支持原生 Windows")
        import ctypes
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        self.callback = callback_type(self.handle)
        self.kernel.SetConsoleCtrlHandler.argtypes = [callback_type, wintypes.BOOL]
        self.kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
        if not self.kernel.SetConsoleCtrlHandler(self.callback, True):
            raise ctypes.WinError(ctypes.get_last_error())
        return self

    def __exit__(self, *exc):
        self.finished.set()
        if self.kernel is not None:
            self.kernel.SetConsoleCtrlHandler(self.callback, False)
