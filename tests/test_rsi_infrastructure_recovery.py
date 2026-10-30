"""Regression checks for the infrastructure faults that stopped RSI training."""
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import types

import pytest

from explore.provisioning import _inventory_from_tgz, _symlink_digest
from benchmarks.osworld.provider import (
    _LoopbackContainers,
    _ensure_guest_bridge_masquerade,
    prepare_checkpointable_docker_provider,
)
from env.qemu_rollback import QemuRollbackError


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stream:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(content)
                stream.addfile(member, io.BytesIO(content))
            else:
                member.linkname = content
                stream.addfile(member)
    return buffer.getvalue()


def test_guest_absolute_symlink_is_hashed_without_host_extraction(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("archive inventory must never extract on the host")
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extract", forbidden)
    manifest, links = _inventory_from_tgz(archive([
        ("evolution_project/venv/bin/python3", tarfile.SYMTYPE, "/usr/bin/python3"),
        ("evolution_project/broken", tarfile.SYMTYPE, "../missing"),
        ("evolution_project/data", tarfile.REGTYPE, b"candidate bytes"),
        ("evolution_project/copy", tarfile.LNKTYPE, "evolution_project/data"),
    ]))
    assert manifest["evolution_project/venv/bin/python3"] == _symlink_digest("/usr/bin/python3")
    assert links["evolution_project/broken"] == "../missing"
    assert manifest["evolution_project/data"] == hashlib.sha256(b"candidate bytes").hexdigest()
    assert manifest["evolution_project/copy"] == manifest["evolution_project/data"]


@pytest.mark.parametrize("entries", [
    [("../escape", tarfile.REGTYPE, b"x")],
    [("/absolute", tarfile.REGTYPE, b"x")],
    [("project/link", tarfile.SYMTYPE, "/tmp"), ("project/link/file", tarfile.REGTYPE, b"x")],
    [("project/file", tarfile.REGTYPE, b"x"), ("project/file", tarfile.REGTYPE, b"y")],
    [("project/link", tarfile.LNKTYPE, "/etc/passwd")],
    [("project/link", tarfile.LNKTYPE, "project/link")],
    [("project/fifo", tarfile.FIFOTYPE, "")],
])
def test_archive_rejects_unsafe_or_ambiguous_member_layout(entries):
    with pytest.raises(ValueError):
        _inventory_from_tgz(archive(entries))


def test_launch_adapter_keeps_release_provider_ports_local(monkeypatch):
    monkeypatch.delenv("RSIAGENT_DOCKER_IMAGE", raising=False)
    calls = []
    container = object()
    nat = []
    collection = types.SimpleNamespace(
        run=lambda *args, **kwargs: calls.append((args, kwargs)) or container,
        list=lambda: ["existing guest"])
    monkeypatch.setattr(
        "benchmarks.osworld.provider._ensure_guest_bridge_masquerade",
        lambda value: nat.append(value),
    )
    class Provider:
        def __init__(self, region):
            self.client = types.SimpleNamespace(containers=collection)
            self.environment = {"RAM_SIZE": "4G"}
    module = types.ModuleType("desktop_env.providers.docker.provider")
    module.DockerProvider = Provider
    package = types.ModuleType("desktop_env.providers.docker")
    package.provider = module
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    prepare_checkpointable_docker_provider("rollback_mirror")
    prepare_checkpointable_docker_provider("rollback_mirror")
    provider = Provider("local")
    original = {5000: 5001, 8006: 8007, 9222: 9223, 8080: 8081}
    provider.client.containers.run("unchanged-image", ports=original, detach=True)
    assert calls == [(("unchanged-image",), {
        "ports": {port: ("127.0.0.1", value) for port, value in original.items()},
        "detach": True})]
    assert original[5000] == 5001
    assert nat == [container]
    assert provider.client.containers.list() == ["existing guest"]
    assert provider.environment["RAM_SIZE"] == "4G"
    assert provider.environment["CPU_FLAGS"].count("migratable=yes") == 1


def test_guest_bridge_masquerade_retries_and_is_idempotent(monkeypatch):
    calls = []

    class Result:
        def __init__(self, exit_code, output=b""):
            self.exit_code = exit_code
            self.output = output

    class Container:
        attrs = {"State": {"Running": True}}

        def exec_run(self, command):
            calls.append(command)
            return Result(75 if len(calls) == 1 else 0, b"bridge not ready")

        def reload(self):
            return None

    monkeypatch.setattr("benchmarks.osworld.provider.time.sleep", lambda _value: None)
    _ensure_guest_bridge_masquerade(Container())

    assert len(calls) == 2
    shell = calls[-1][2]
    assert "iptables -t nat -C POSTROUTING" in shell
    assert "-o dockerbridge" in shell
    assert "-d 20.20.20.21/32" in shell
    assert "iptables -t nat -I POSTROUTING 1" in shell


@pytest.mark.parametrize("field,value", [
    ("boot_timeout", 0),
    ("boot_timeout", 1.5),
    ("boot_attempts", 0),
    ("boot_attempts", 3),
])
def test_checkpointable_provider_rejects_invalid_boot_policy(field, value):
    kwargs = {field: value}
    with pytest.raises(ValueError, match=field):
        prepare_checkpointable_docker_provider("rollback_mirror", **kwargs)


@pytest.mark.parametrize("override", [
    "registry.example/rsiagent/osworld@sha256:" + "a" * 64,
    "sha256:" + "b" * 64,
])
def test_docker_image_override_accepts_only_pinned_references(monkeypatch, override):
    calls = []
    collection = types.SimpleNamespace(
        run=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setenv("RSIAGENT_DOCKER_IMAGE", override)

    _LoopbackContainers(collection).run("happysixd/osworld-docker", detach=True)

    assert calls == [((override,), {"detach": True})]


def test_docker_image_override_supports_expected_keyword_argument(monkeypatch):
    override = "local/osworld@sha256:" + "c" * 64
    calls = []
    collection = types.SimpleNamespace(
        run=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setenv("RSIAGENT_DOCKER_IMAGE", override)

    _LoopbackContainers(collection).run(
        image="happysixd/osworld-docker", detach=True
    )

    assert calls == [((), {"image": override, "detach": True})]


def test_docker_image_override_does_not_replace_unrelated_images(monkeypatch):
    override = "local/osworld@sha256:" + "d" * 64
    calls = []
    collection = types.SimpleNamespace(
        run=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setenv("RSIAGENT_DOCKER_IMAGE", override)

    _LoopbackContainers(collection).run("another/image", detach=True)

    assert calls == [(("another/image",), {"detach": True})]


@pytest.mark.parametrize("override", [
    "happysixd/osworld-docker:latest",
    "happysixd/osworld-docker@sha256:" + "a" * 63,
    "happysixd/osworld-docker@sha256:" + "A" * 64,
    "happysixd/osworld-docker:legacy@sha256:" + "a" * 64,
    "happysixd//osworld-docker@sha256:" + "a" * 64,
    " happysixd/osworld-docker@sha256:" + "a" * 64,
])
def test_docker_image_override_rejects_unpinned_or_noncanonical_values(
        monkeypatch, override):
    calls = []
    collection = types.SimpleNamespace(
        run=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setenv("RSIAGENT_DOCKER_IMAGE", override)

    with pytest.raises(QemuRollbackError, match="RSIAGENT_DOCKER_IMAGE"):
        _LoopbackContainers(collection).run("happysixd/osworld-docker")

    assert calls == []


def test_custom_csi_outer_image_is_digest_and_package_pinned():
    dockerfile = (
        Path(__file__).resolve().parents[1] / "custom_csi/docker/Dockerfile"
    ).read_text(encoding="utf-8")

    assert dockerfile.startswith(
        "FROM happysixd/osworld-docker@sha256:"
        "0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9\n"
    )
    assert "ebtables=2.0.11-6" in dockerfile
    assert (
        "update-alternatives --set ebtables /usr/sbin/ebtables-legacy"
        in dockerfile
    )
    assert "/usr/sbin/ebtables-legacy-save" in dockerfile
    assert "/usr/sbin/ebtables-legacy-restore" in dockerfile
