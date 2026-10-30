"""Failed-boot evidence separates GUI, QEMU and host HTTP readiness failures."""
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

from PIL import Image
import pytest

from benchmarks.osworld import boot_diagnostics as diagnostics


def screenshot_archive(kind=tarfile.REGTYPE):
    pixels = io.BytesIO()
    Image.new("RGB", (3, 2), (20, 40, 60)).save(pixels, format="PPM")
    data = pixels.getvalue()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo("rsiagent-boot-screen.ppm")
        entry.type = kind
        entry.size = len(data) if kind == tarfile.REGTYPE else 0
        entry.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
        archive.addfile(entry, io.BytesIO(data) if entry.size else None)
    return iter([buffer.getvalue()]), {}


def test_qemu_screen_keeps_guest_pixels_without_extracting_paths(tmp_path):
    calls = []
    def run(command):
        calls.append(command)
        return SimpleNamespace(exit_code=0, output=b"(qemu) screenshot saved\n(qemu)")
    container = SimpleNamespace(exec_run=run, get_archive=lambda _: screenshot_archive())
    result = diagnostics._qemu_screen(container, tmp_path)
    with Image.open(tmp_path / result["file"]) as image:
        assert image.size == (3, 2)
        assert image.getpixel((0, 0)) == (20, 40, 60)
    assert calls[0][-2:] == ["screendump /tmp/rsiagent-boot-screen.ppm", "5"]


def test_qemu_screen_rejects_archive_symlinks(tmp_path):
    container = SimpleNamespace(
        exec_run=lambda _: (0, b"(qemu)"),
        get_archive=lambda _: screenshot_archive(tarfile.SYMTYPE))
    with pytest.raises(ValueError, match="regular file"):
        diagnostics._qemu_screen(container, tmp_path)
    assert not (tmp_path / "boot-screen.png").exists()


def test_http_checks_distinguish_proxy_from_direct_loopback(tmp_path, monkeypatch):
    calls = []
    class Response:
        headers = {"Content-Type": "image/png"}
        def __init__(self, status): self.status_code = status
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, self.trust_env, kwargs))
            return Response(502 if self.trust_env else 200)
    monkeypatch.setattr(diagnostics.requests, "Session", Session)
    rows = diagnostics._http_checks(5007, tmp_path)
    assert [row["status"] for row in rows] == [502, 200, 200]
    assert [call[0] for call in calls] == [
        "http://localhost:5007/screenshot", "http://localhost:5007/screenshot",
        "http://127.0.0.1:5007/screenshot"]
    assert all(call[2] == {"timeout": (2, 3), "allow_redirects": False, "stream": True}
               for call in calls)
    assert json.loads((tmp_path / "http_checks.json").read_text()) == rows


def test_failed_probe_does_not_hide_other_evidence_or_save_secrets(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("http://username:private-password@proxy.invalid")
    monkeypatch.setattr(diagnostics, "_http_checks", fail)
    monkeypatch.setattr(diagnostics, "_qemu_screen", fail)
    container = SimpleNamespace(
        attrs={"Config": {"Image": "sha256:" + "a" * 64,
                          "Env": ["CPU_MODEL=host", "KVM=N", "CSI_API_KEY=private-key"]},
               "HostConfig": {"Devices": []}},
        exec_run=lambda command: (0, b"running"))
    provider = SimpleNamespace(container=container, server_port=5000)
    result = diagnostics.collect_boot_diagnostics(provider, tmp_path)
    saved = (tmp_path / "diagnostics.json").read_text()
    assert "private-password" not in saved and "private-key" not in saved
    assert result["launch"]["environment"] == {"CPU_MODEL": "host", "KVM": "N"}
    assert result["http"] == {"error_type": "RuntimeError"}
    assert result["info_status"]["output"] == "running"
    assert result["screen"] == {"error_type": "RuntimeError"}


def test_failed_extra_diagnostics_still_cleans_owned_guest(tmp_path, monkeypatch):
    import os
    from benchmarks.osworld import provider as adapter
    calls = []
    class Container:
        id = "owned-container"
        labels = {"rsiagent.owner": "smoke-owner", "rsiagent.host_pid": str(os.getpid()),
                  "rsiagent.launch_token": "test-launch"}
        attrs = {"State": {"Status": "running"}}
        def reload(self): pass
        def logs(self, **kwargs): return b"firmware boot log"
        def remove(self, **kwargs): calls.append(kwargs)
    def fail(*args): raise RuntimeError("diagnostics failed")
    monkeypatch.setattr(diagnostics, "collect_boot_diagnostics", fail)
    monkeypatch.setenv("RSIAGENT_BOOT_DIAGNOSTICS_DIR", str(tmp_path))
    provider = SimpleNamespace(container=Container())
    adapter._archive_failed_boot(provider, "smoke-owner", 600)
    receipt = json.loads(next(tmp_path.rglob("receipt.json")).read_text())
    assert receipt["diagnostic_error_type"] == "RuntimeError"
    assert receipt["removed_with_anonymous_volume"] is True
    assert provider.container is None
    assert calls == [{"force": True, "v": True}]


def test_guest_probe_failure_still_preserves_screenshot_and_network_rules():
    calls = []
    def run(command):
        calls.append(command)
        if command[-1] == "http://20.20.20.21:5000/":
            raise RuntimeError("unavailable")
        if command[-1] == "http://20.20.20.21:5000/screenshot":
            return 0, b"http_code=200 remote_ip=20.20.20.21\n"
        return 0, b"-A PREROUTING -p tcp --dport 5000 -j DNAT\n"
    result = diagnostics._container_network_checks(SimpleNamespace(exec_run=run))
    assert result["probes"]["guest_root"] == {"error_type": "RuntimeError"}
    assert "http_code=200" in result["probes"]["guest_screenshot"]["output"]
    assert "DNAT" in result["probes"]["network_state"]["output"]
    for command in calls[:2]:
        assert command[command.index("--noproxy") + 1] == "*"
        assert "--max-time" in command


def test_short_boot_diagnostic_archives_and_cleans_without_second_launch(monkeypatch):
    from contextlib import nullcontext
    from benchmarks.osworld import provider as adapter
    calls = []
    class Container:
        def remove(self, **kwargs): calls.append(("remove", kwargs))
    class Provider:
        container = None
        def _get_used_ports(self): return set()
        def _wait_for_vm_ready(self, timeout):
            calls.append(("wait", timeout))
            raise TimeoutError("VM failed to become ready within timeout period")
        def start_emulator(self):
            calls.append("start")
            self.container = Container()
            self._wait_for_vm_ready()
    def archive(provider, owner, timeout):
        calls.append(("archive", owner, timeout))
        provider.container.remove(force=True, v=True)
        provider.container = None
    monkeypatch.setenv("RSIAGENT_VM_OWNER", "diagnostic-owner")
    monkeypatch.setattr(adapter, "_boot_slot", nullcontext)
    monkeypatch.setattr(adapter, "_archive_failed_boot", archive)
    adapter._prepare_boot_lifecycle(
        SimpleNamespace(DockerProvider=Provider), boot_timeout=120, boot_attempts=1)
    provider = Provider()
    with pytest.raises(TimeoutError):
        provider.start_emulator()
    assert calls == ["start", ("wait", 120), ("archive", "diagnostic-owner", 120),
                     ("remove", {"force": True, "v": True})]
    assert provider.container is None


@pytest.mark.parametrize("options", [
    {"boot_timeout": 0}, {"boot_timeout": -1}, {"boot_timeout": True},
    {"boot_attempts": 0}, {"boot_attempts": 3}, {"boot_attempts": 1.5},
])
def test_invalid_boot_policy_rejected_before_launch(options):
    from benchmarks.osworld import provider as adapter
    with pytest.raises(ValueError):
        adapter._prepare_boot_lifecycle(SimpleNamespace(), **options)
