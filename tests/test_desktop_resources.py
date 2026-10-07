"""P0 package-resource checks use only fresh data; never contact a provider/worker."""
from pathlib import Path
import runpy

import pytest


PROBE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "probe-desktop-runtime.py"))


def test_runtime_resources_are_available_without_live_services(tmp_path):
    report = PROBE["probe"](tmp_path / "runtime paths with spaces")
    assert report["status"] == "ok"
    assert report["codex_version"].endswith(report["dependencies"]["openai-codex-cli-bin"])
    assert "cross-process lock rejection/reacquisition" in report["checks"]
    assert "provider connection or inference" in report["not_verified"]
    assert list((tmp_path / "runtime paths with spaces").iterdir()) == []


def test_offline_guard_rejects_credentials_and_provider_work():
    for operation in ("get", "save", "remove", "status", "select", "finish"):
        with pytest.raises(AssertionError, match="forbidden operation"):
            getattr(PROBE["OfflineOnly"](), operation)
