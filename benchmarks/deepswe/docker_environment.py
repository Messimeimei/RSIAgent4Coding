"""Pier Docker extension for model gateways on nonstandard HTTP(S) ports."""

import asyncio
import json
import re
import time

from pier.environments.base import ExecResult
from pier.environments.docker.docker import (
    DockerEnvironment, _sanitize_docker_compose_project_name,
)

from .docker_lifecycle import (
    cleanup_environment, network_conflict, network_lifecycle_lock, run_compose_process,
)
from .docker_network import allocate_subnets, automatic_networks, network_inventory


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
    @property
    def _docker_compose_paths(self):
        paths = super()._docker_compose_paths
        if getattr(self, '_network_ipam_path', None):
            paths.append(self._network_ipam_path)
        return paths

    def _compose_env(self):
        env = self._env_vars.to_env_dict(include_os_env=True)
        env.update(self._compose_task_env or {})
        env.update(self._persistent_env or {})
        if self._windows_container_name:
            env['PIER_CONTAINER_NAME'] = self._windows_container_name
        return env

    async def _prepare_network_ipam(self, attempt, excluded):
        # Inspect the merged task definition without our previous override. Do
        # not log resolved Compose config: it includes proxy authentication.
        self._network_ipam_path = None
        result = await self._compose_once(['config', '--format', 'json'], check=False)
        if result.return_code:
            raise RuntimeError('Cannot inspect Compose networks before RSI startup')
        config = json.loads(result.stdout)
        project = _sanitize_docker_compose_project_name(self.session_id)
        managed = automatic_networks(config, project)
        if not managed:
            # In particular the official grader keeps network_mode: none.
            self._lifecycle_event(event='network_plan', attempt=attempt, subnets={})
            return {}
        occupied, inventory = await network_inventory(self._compose_env())
        plan = {}
        for key in managed:
            name = f'{project}_{key}'
            existing = next((n for n in inventory if n['name'] == name), None)
            if existing:
                if (existing['project'] != project or existing['key'] != key
                        or existing['driver'] != 'bridge' or len(existing['subnets']) != 1):
                    raise RuntimeError(f'Cannot safely reuse Compose network {name}')
                # Idempotent up must preserve an already started environment.
                plan[key] = existing['subnets'][0]
        plan.update(allocate_subnets(project, managed.keys() - plan.keys(), occupied,
                                     excluded=excluded))
        path = self.trial_paths.trial_dir / 'docker-compose-network-ipam.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'networks': {
            key: {'ipam': {'config': [{'subnet': subnet}]}}
            for key, subnet in plan.items()}}, indent=2) + '\n')
        temporary.replace(path)
        self._network_ipam_path = path
        self._lifecycle_event(event='network_plan', attempt=attempt, subnets=plan,
            excluded=sorted(excluded), occupied=[str(net) for net in occupied],
            docker_networks=inventory)
        return plan

    async def stop(self, delete):
        # Pier's default stop skips down if log adjustment fails, and discards
        # compose state even when down fails. Apply the same scoped, retryable
        # cleanup to the final Actor and official grader as to learning VMs.
        command = ['stop'] if self._keep_containers else None if delete else ['down']
        await cleanup_environment(self, command=command)

    def _lifecycle_event(self, **record):
        path = self.trial_paths.trial_dir / 'docker-lifecycle.jsonl'
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a') as stream:
                stream.write(json.dumps({'time': time.time(), 'project': self.session_id,
                                         **record}) + '\n')
        except OSError:
            # A full log disk cannot prevent cleanup of a partially created
            # project, nor replace the original Docker failure.
            pass

    async def _compose_once(self, command, check=True, timeout_sec=None):
        args = ['docker', 'compose', '--project-name',
                _sanitize_docker_compose_project_name(self.session_id),
                '--project-directory', str(self.environment_dir.resolve())]
        for path in self._docker_compose_paths:
            args.extend(['-f', str(path.resolve())])
        args.extend(command)
        env = self._compose_env()
        # Compose can create the two proxy networks concurrently even within
        # one project. Serialize its engine calls as well as projects.
        if command[0] in {'up', 'down'}:
            env['COMPOSE_PARALLEL_LIMIT'] = '1'
        # Long-lived BitFun exec is supervised by its per-role work clock.
        # A lifecycle default must not silently cap it at ten minutes.
        timeout = timeout_sec if command[0] == 'exec' else timeout_sec or 600
        output_options = {'stdout_only': True} if command[0] == 'config' else {}
        code, output = await run_compose_process(args, env, timeout, **output_options)
        if check and code:
            raise RuntimeError(f'Docker compose {command[0]} failed for '
                               f'{self.session_id} (exit {code}): {output}')
        return ExecResult(return_code=code, stdout=output, stderr=None)

    async def _run_docker_compose_command(self, command, check=True, timeout_sec=None):
        command = list(command)
        # Images are shared dependencies, unlike this unique compose project.
        # Avoid deleting the task image when a review or final grader exits.
        if command[0] == 'down' and '--rmi' in command:
            index = command.index('--rmi')
            del command[index:index + 2]
        if command[0] not in {'up', 'down'}:
            return await self._compose_once(command, check, timeout_sec)
        async with network_lifecycle_lock():
            if command[0] == 'down':
                return await self._compose_once(command, check, timeout_sec)
            excluded = set()
            for attempt in range(1, 4):
                result = None
                plan = {}
                try:
                    # Keep query, allocation and creation inside the same lock.
                    # Non-RSI Docker clients can still race; a collision below
                    # refreshes inventory AND excludes all attempted subnets.
                    plan = await self._prepare_network_ipam(attempt, excluded)
                    result = await self._compose_once(command, False, timeout_sec)
                    if result.return_code:
                        raise RuntimeError(f'Docker compose up failed for {self.session_id} '
                            f'(exit {result.return_code}): {result.stdout}')
                    self._lifecycle_event(event='started', attempt=attempt, subnets=plan)
                    return result
                except BaseException as error:
                    conflict = isinstance(error, RuntimeError) and network_conflict(str(error))
                    self._lifecycle_event(event='start_failed', attempt=attempt,
                        error_type=type(error).__name__, network_conflict=conflict,
                        subnets=plan, conflict_network_ids=(
                            re.findall(r'\b[0-9a-f]{64}\b', str(error)) if conflict else []))
                    # No Agent has run in this environment yet. Remove only
                    # this attempt's partial resources before retry/cancellation.
                    try:
                        await self._compose_once(['down', '--volumes', '--remove-orphans'],
                                                 timeout_sec=120)
                    except Exception as cleanup_error:
                        error.add_note(f'Scoped startup cleanup failed: {cleanup_error}')
                        raise error
                    if not conflict or attempt == 3:
                        if not check and result is not None:
                            return result
                        raise
                    excluded.update(plan.values())
                    self._lifecycle_event(event='network_conflict_retry', attempt=attempt,
                                          excluded=sorted(excluded))
                    await asyncio.sleep(attempt)

    def __init__(self, *args, inference_ports=None, **kwargs):
        self._network_ipam_path = None
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
