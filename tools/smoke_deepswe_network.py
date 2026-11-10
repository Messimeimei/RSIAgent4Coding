#!/usr/bin/env python3
"""Real Docker IPAM collision recovery and repeated parallel RSI environments.

Uses a local HTTP stub, no model calls. Only removes Compose projects and the
one race-simulation network created by this invocation.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import shlex
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.deepswe.docker_environment import BitfunDockerEnvironment
from benchmarks.deepswe.docker_lifecycle import run_compose_process
from pier.models.agent.network import NetworkAllowlist
from pier.models.task.task import Task
from pier.models.trial.paths import TrialPaths
from run_deepswe import load_job


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'rsi-network-smoke-ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def docker(*args):
    code, output = await run_compose_process(['docker', *args], dict(os.environ), 120)
    if code:
        raise RuntimeError(f'Docker smoke command {args[:2]} failed (exit {code})')
    return output


async def smoke(args):
    destination = ROOT / 'results/deepswe/smoke' / args.name
    destination.mkdir(parents=True, exist_ok=False)
    config = load_job(args.config, mode='rsi')
    task = Task(config.datasets[0].path / 'dateutil-rfc5545-timezone-interop')
    token = uuid.uuid4().hex[:12]
    protected_format = '{{.Id}} {{.State.StartedAt}} {{.State.Running}}'
    before_production = await docker('inspect', '--format', protected_format, 'thinkingcap-qwen38-opt-15')
    before_networks = set((await docker('network', 'ls', '--quiet', '--no-trunc')).split())
    environments, created_names = [], []
    blocker = f'rsi-net-smoke-{token}-race'
    blocker_id = None
    checks, plans = {}, []

    def environment(index, *, isolated=False):
        project = f'rsi-net-smoke-{token}-{index}'
        paths = TrialPaths(destination / str(index))
        paths.mkdir()
        env = BitfunDockerEnvironment(
            environment_dir=task.paths.environment_dir, environment_name=task.name,
            session_id=project, trial_paths=paths,
            task_env_config=task.config.environment.model_copy(deep=True),
            network_allowlist=NetworkAllowlist(domains=[] if isolated else [args.bind_host]),
            inference_ports=[args.port], mounts_json=config.environment.mounts)
        environments.append(env)
        created_names.append(project)
        return env

    async def verify_proxy(env):
        # Explicitly exercise the exact Pier proxy auth and extra-port policy.
        script = (
            "import urllib.request; "
            "proxy=urllib.request.ProxyHandler(); "
            "opener=urllib.request.build_opener(proxy); "
            f"assert opener.open('http://{args.bind_host}:{args.port}/probe',timeout=5).read()==b'rsi-network-smoke-ok'"
        )
        result = await env.exec(command='python3 -c ' + shlex.quote(script),
                                env=env.agent_process_env({}))
        if result.return_code:
            raise AssertionError(f'Proxy request failed in {env.session_id}')
        script = (
            "import socket; "
            f"s=socket.socket(); s.settimeout(1); code=s.connect_ex(({args.bind_host!r},{args.port})); "
            "s.close(); assert code != 0, 'Actor unexpectedly bypassed proxy'"
        )
        result = await env.exec(command='python3 -c ' + shlex.quote(script))
        assert result.return_code == 0, 'Internal bridge lost egress isolation'

    async def live_networks(env):
        ids = (await docker('network', 'ls', '--quiet', '--no-trunc', '--filter',
                            f'label=com.docker.compose.project={env.session_id}')).split()
        assert len(ids) == 2
        records = json.loads(await docker('network', 'inspect', *ids))
        actual = {}
        for record in records:
            key = record['Labels']['com.docker.compose.network']
            actual[key] = record['IPAM']['Config'][0]['Subnet']
            assert record['Internal'] == (key == 'pier-egress-internal')
        plans.append(actual)
        return actual

    failure = None
    try:
        env = environment('collision')
        original = env._prepare_network_ipam
        attempted = []
        async def race_once(attempt, excluded):
            nonlocal blocker_id
            plan = await original(attempt, excluded)
            attempted.append(plan.copy())
            if attempt == 1:
                # Reproduce a different Docker client winning the create race
                # AFTER allocation but BEFORE compose up. Never overlaps an
                # existing host route: the adapter just checked this subnet.
                blocker_id = (await docker('network', 'create', '--driver', 'bridge',
                    '--internal', '--subnet', plan['pier-egress-internal'],
                    '--label', f'rsi.network-smoke={token}', blocker)).strip()
            return plan
        env._prepare_network_ipam = race_once
        await env.start(force_build=False)
        actual = await live_networks(env)
        await verify_proxy(env)
        checks['real_collision_recovered_with_new_subnets'] = (
            len(attempted) == 2 and not (set(attempted[0].values()) & set(attempted[1].values()))
            and actual == attempted[1])
        # Cleanup after the first failed up must leave the other owner's network.
        checks['scoped_cleanup_preserved_competing_network'] = (
            (await docker('network', 'inspect', '--format', '{{.Id}}', blocker)).strip() == blocker_id)
        await env.stop(delete=True)
        await docker('network', 'rm', blocker_id)
        blocker_id = None

        for wave in range(args.rounds):
            batch = [environment(f'{wave}-{branch}') for branch in range(args.parallel)]
            await asyncio.gather(*(item.start(force_build=False) for item in batch))
            networks = await asyncio.gather(*(live_networks(item) for item in batch))
            values = [ipaddress.ip_network(subnet) for plan in networks for subnet in plan.values()]
            assert all(not left.overlaps(right) for i, left in enumerate(values) for right in values[i + 1:])
            await asyncio.gather(*(verify_proxy(item) for item in batch))
            if wave == 0:
                await batch[0]._run_docker_compose_command(['up', '--detach', '--wait'])
                checks['idempotent_up_preserves_subnets'] = await live_networks(batch[0]) == networks[0]
            await asyncio.gather(*(item.stop(delete=True) for item in batch))
            print(json.dumps({'round': wave + 1, 'rounds': args.rounds,
                'parallel': args.parallel, 'status': 'passed'}), flush=True)
        checks['parallel_start_reset_cycles'] = True
        checks['proxy_works_and_direct_egress_blocked'] = True

        isolated = environment('official-none', isolated=True)
        await isolated.start(force_build=False)
        ids = (await docker('ps', '--quiet', '--filter',
                            f'label=com.docker.compose.project={isolated.session_id}')).split()
        assert len(ids) == 1
        checks['official_container_has_no_network'] = (
            (await docker('inspect', '--format', '{{.HostConfig.NetworkMode}}', ids[0])).strip() == 'none'
            and not (await docker('network', 'ls', '--quiet', '--filter',
                                  f'label=com.docker.compose.project={isolated.session_id}')).strip())
        await isolated.stop(delete=True)
    except BaseException as error:
        failure = error
    finally:
        cleanup_errors = []
        for env in environments:
            try:
                # Repeated down is safe; only this invocation's unique project.
                await env._run_docker_compose_command(['down', '--volumes', '--remove-orphans'])
            except Exception as error:
                cleanup_errors.append(f'{env.session_id}: {type(error).__name__}')
        if blocker_id:
            try:
                await docker('network', 'rm', blocker_id)
            except Exception as error:
                cleanup_errors.append(f'blocker: {type(error).__name__}')
        after = set((await docker('network', 'ls', '--quiet', '--no-trunc')).split())
        checks['preexisting_networks_preserved'] = before_networks.issubset(after)
        checks['production_container_unchanged'] = before_production == await docker(
            'inspect', '--format', protected_format, 'thinkingcap-qwen38-opt-15')
        remaining = await docker('ps', '--all', '--format', '{{.Names}}')
        remaining_networks = await docker('network', 'ls', '--format', '{{.Name}}')
        checks['no_smoke_resources_left'] = not any(name in remaining + remaining_networks for name in created_names)
        checks['cleanup_succeeded'] = not cleanup_errors
        receipt = {'status': 'passed' if not failure and all(checks.values()) else 'failed',
            'paid_model_calls': 0, 'environments': len(environments), 'checks': checks,
            'plans': plans, 'cleanup_errors': cleanup_errors,
            'error_type': type(failure).__name__ if failure else None}
        (destination / 'smoke-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2), flush=True)
    if failure:
        raise failure
    return 0 if receipt['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/deepswe/rsi.yaml')
    parser.add_argument('--rounds', type=int, default=12)
    parser.add_argument('--parallel', type=int, default=3)
    parser.add_argument('--bind-host', default='169.254.30.1')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if Path(args.name).name != args.name or args.name in {'.', '..'}:
        parser.error('name must be a single directory name')
    if args.rounds < 1 or not 1 <= args.parallel <= 4:
        parser.error('require rounds >= 1 and parallel between 1 and 4')
    os.chdir(ROOT)
    os.umask(0o077)
    with ThreadingHTTPServer((args.bind_host, args.port), Stub) as server:
        args.port = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            return asyncio.run(smoke(args))
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
