"""Native runtime admission; WSL is a development environment, not a backend."""

import os
import platform


def require_supported_host() -> None:
    if os.name == "nt":
        return
    if "microsoft" in platform.release().lower() or os.environ.get("WSL_DISTRO_NAME"):
        raise RuntimeError("不支持 WSL 混合运行；请在 Windows Python 中启动程序")
    if platform.system() == "Linux":
        raise RuntimeError("Linux 尚无受支持的原生 MuMu 后端，无法启动；不使用 Wine 或远程 Windows")
    raise RuntimeError("仅支持原生 Windows MuMu；当前系统没有受支持的模拟器后端")
