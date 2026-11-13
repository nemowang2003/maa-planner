from types import SimpleNamespace

import pytest

from maa_planner.daemon.adapters import host


@pytest.mark.parametrize(
    "name,system,release,message",
    [
        ("posix", "Linux", "microsoft-standard-WSL2", "WSL"),
        ("posix", "Linux", "6.12", "Linux"),
        ("posix", "Darwin", "24", "原生 Windows"),
    ],
)
def test_unsupported_host_is_rejected_before_runtime_work(
    monkeypatch, name, system, release, message
):
    monkeypatch.setattr(host, "os", SimpleNamespace(name=name, environ={}))
    monkeypatch.setattr(host.platform, "system", lambda: system)
    monkeypatch.setattr(host.platform, "release", lambda: release)
    with pytest.raises(RuntimeError, match=message):
        host.require_supported_host()


def test_native_windows_is_admitted(monkeypatch):
    monkeypatch.setattr(host, "os", SimpleNamespace(name="nt", environ={}))
    host.require_supported_host()
