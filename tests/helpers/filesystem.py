"""Native filesystem fixtures; Windows junctions do not require symlink privileges."""

import subprocess
from contextlib import contextmanager


@contextmanager
def windows_junction(path, target):
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    subprocess.run(
        [
            "powershell.exe",
            "-Command",
            f"New-Item -ItemType Junction -Path {quote(path)} -Target {quote(target)} | Out-Null",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield path
    finally:
        path.rmdir()
