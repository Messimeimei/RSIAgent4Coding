"""Pier Docker extension for model gateways on nonstandard HTTP(S) ports."""

from pier.environments.docker.docker import DockerEnvironment


def proxy_script_with_ports(script: str, ports: list[int]) -> str:
    for port in ports:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("inference_ports must contain TCP port numbers")
    if not ports:
        return script
    replacements = {
        "acl SSL_ports port 443": "acl SSL_ports port "
        + " ".join(map(str, sorted({443, *ports}))),
        "acl Safe_ports port 80 443": "acl Safe_ports port "
        + " ".join(map(str, sorted({80, 443, *ports}))),
    }
    for before, after in replacements.items():
        if script.count(before) != 1:
            raise RuntimeError(
                "Pinned Pier proxy template changed; refusing to weaken policy"
            )
        script = script.replace(before, after)
    return script


class BitfunDockerEnvironment(DockerEnvironment):
    def __init__(self, *args, inference_ports=None, **kwargs):
        self._inference_ports = list(inference_ports or [])
        super().__init__(*args, **kwargs)
        # In this Pier version explicit mounts replace the defaults. Binary mounts
        # must be additive or logs and the submitted patch vanish on container stop.
        if self.network_allowlist.domains:
            mounted_targets = {mount["target"] for mount in self._mounts_json}
            self._mounts_json = [
                *self._mounts_json,
                *(
                    mount
                    for mount in self._default_log_mounts()
                    if mount["target"] not in mounted_targets
                ),
            ]
        # The official verifier deliberately mounts ONLY verifier logs. Its
        # artifacts are uploaded by Pier into a fresh container, not shared back
        # with the host candidate directory (which upload_artifacts empties).

    def _prepare_egress_proxy_compose(self):
        super()._prepare_egress_proxy_compose()
        if self._egress_proxy_compose_path is None:
            return
        # Change only generated run-local policy; domain ACL and authentication
        # remain Pier-owned. The official no-network verifier has no egress proxy.
        script = self.trial_paths.trial_dir / "egress-proxy/start-squid.sh"
        script.write_text(
            proxy_script_with_ports(script.read_text(), self._inference_ports)
        )
