"""Collect evidence from an owned, disposable guest before failed-boot cleanup.

No task or model has run at this point. Diagnostic failures must not prevent
the existing cleanup/retry policy from running.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile

import requests

from env.qemu_rollback import _HMP_SCRIPT, _clean_hmp_output


def _container_network_checks(container) -> dict:
    """Probe the guest without traversing the host's published Docker port.

    The pinned qemu-docker image uses 20.20.20.21 by default. Keep the target
    explicit in the receipt; a DHCP lease with another address is evidence of
    a configuration mismatch, not proof the guest controller is down.
    """
    checks = {
        "guest_root": [
            "curl", "--noproxy", "*", "--silent", "--show-error",
            "--connect-timeout", "2", "--max-time", "5", "--output", "/dev/null",
            "--write-out", "http_code=%{http_code} remote_ip=%{remote_ip}\n",
            "http://20.20.20.21:5000/"],
        "guest_screenshot": [
            "curl", "--noproxy", "*", "--silent", "--show-error",
            "--connect-timeout", "2", "--max-time", "8", "--output", "/dev/null",
            "--write-out", "http_code=%{http_code} remote_ip=%{remote_ip}\n",
            "http://20.20.20.21:5000/screenshot"],
        "network_state": ["timeout", "15", "sh", "-c", "\n".join([
            "ip -4 route get 20.20.20.21",
            "ip neigh show",
            "bridge link show",
            "cat /proc/sys/net/ipv4/ip_forward",
            "cat /proc/sys/net/bridge/bridge-nf-call-iptables",
            "cat /var/lib/misc/dnsmasq.leases",
            "iptables --version",
            "iptables-save -c",
            "iptables-nft-save -c",
            "iptables-legacy-save -c",
            "ebtables-save",
        ])],
    }
    result = {"guest_address": "20.20.20.21", "probes": {}}
    for name, command in checks.items():
        try:
            result["probes"][name] = _exec(container, command)
        except Exception as exc:
            result["probes"][name] = {"error_type": type(exc).__name__}
    return result


def _exec(container, command: list[str]) -> dict:
    result = container.exec_run(command)
    if hasattr(result, "exit_code"):
        code, output = result.exit_code, result.output
    else:
        code, output = result
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    return {"exit_code": code, "output": str(output or "")[-65536:]}


def _http_checks(port: int, destination: Path) -> list[dict]:
    records = []
    # Match the upstream check first; compare with explicit loopback requests
    # that bypass proxy configuration. Do not log proxy URLs or credentials.
    for host, trust_env in (("localhost", True), ("localhost", False),
                            ("127.0.0.1", False)):
        record = {"host": host, "port": port, "trust_env": trust_env}
        try:
            with requests.Session() as session:
                session.trust_env = trust_env
                with session.get(
                        f"http://{host}:{port}/screenshot", timeout=(2, 3),
                        allow_redirects=False, stream=True) as response:
                    record.update(status=response.status_code,
                                  content_type=response.headers.get("Content-Type", ""))
                    # Only status/headers are required; don't buffer an arbitrary
                    # proxy error body or a potentially stalled screenshot stream.
        except Exception as exc:
            record["error_type"] = type(exc).__name__
        records.append(record)
    (destination / "http_checks.json").write_text(
        json.dumps(records, indent=2) + "\n", encoding="utf-8")
    return records


def _qemu_screen(container, destination: Path) -> dict:
    screen_path = "/tmp/rsiagent-boot-screen.ppm"
    result = _exec(container, ["bash", "-c", _HMP_SCRIPT,
                              "rsiagent-boot-diagnostics",
                              f"screendump {screen_path}", "5"])
    result["output"] = _clean_hmp_output(result["output"])
    if result["exit_code"] != 0:
        return result
    chunks, _ = container.get_archive(screen_path)
    data = bytearray()
    try:
        for chunk in chunks:
            data.extend(chunk)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("boot screenshot exceeds 32 MiB")
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()
    # Read only the expected file from the archive, never extract paths.
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.getmember("rsiagent-boot-screen.ppm")
        if not member.isfile():
            raise ValueError("boot screenshot is not a regular file")
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError("boot screenshot has no content")
        from PIL import Image

        with stream, Image.open(stream) as screenshot:
            screenshot.save(destination / "boot-screen.png")
    result["file"] = "boot-screen.png"
    return result


def collect_boot_diagnostics(provider, destination: Path) -> dict:
    """Called only after the provider has checked container ownership."""
    container = provider.container
    report = {}
    config = container.attrs.get("Config", {})
    allowed = {"CPU_MODEL", "CPU_FLAGS", "CPU_CORES", "RAM_SIZE", "DISK_SIZE",
               "BOOT_MODE", "ARGUMENTS", "KVM", "NETWORK", "DHCP", "VM_NET_IP"}
    environment = {}
    for item in config.get("Env") or []:
        name, _, value = item.partition("=")
        if name in allowed:
            environment[name] = value
    report["launch"] = {
        "image": config.get("Image"),
        "environment": environment,
        "devices": container.attrs.get("HostConfig", {}).get("Devices"),
        "port_bindings": container.attrs.get("HostConfig", {}).get("PortBindings"),
    }
    checks = {
        "http": lambda: _http_checks(provider.server_port, destination),
        "container_system": lambda: _exec(container, [
            "timeout", "15", "sh", "-c",
            "ls -l /dev/kvm\nps -eo pid,comm,args\nip -br addr\nip route\nss -lnt"]),
        "container_network": lambda: _container_network_checks(container),
    }
    for command in ("info status", "info kvm", "info network"):
        checks[command.replace(" ", "_")] = lambda command=command: _exec(
            container, ["bash", "-c", _HMP_SCRIPT,
                        "rsiagent-boot-diagnostics", command, "5"])
    checks["screen"] = lambda: _qemu_screen(container, destination)
    for name, check in checks.items():
        try:
            report[name] = check()
        except Exception as exc:
            # Exception messages may contain Docker or proxy connection URLs.
            report[name] = {"error_type": type(exc).__name__}
    (destination / "diagnostics.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
