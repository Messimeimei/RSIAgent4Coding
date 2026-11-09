"""Avoid the repeated automatic-IPAM collision which aborted a real BRS wave."""

import asyncio
import ipaddress
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pier.environments.base import ExecResult

from benchmarks.deepswe.docker_environment import BitfunDockerEnvironment
from benchmarks.deepswe.docker_network import (
    allocate_subnets, automatic_networks, network_inventory, occupied_subnets,
)


def proxy_config():
    return {'services': {
        'main': {'networks': {'pier-egress-internal': {}}},
        'proxy': {'networks': {'pier-egress-internal': {}, 'default': {}}}},
        'networks': {'pier-egress-internal': {'internal': True}, 'default': {}}}


def test_allocation_excludes_docker_routes_interfaces_and_failed_plans():
    occupied = occupied_subnets(
        [{'IPAM': {'Config': [{'Subnet': '10.0.0.0/9'}, {'Subnet': 'fd00::/64'}]}}],
        [{'dst': 'default'}, {'dst': '10.128.0.0/10', 'table': 301},
         {'dst': '10.192.0.0/11', 'type': 'unreachable'}],
        [{'addr_info': [{'family': 'inet', 'local': '10.224.0.1', 'prefixlen': 12}]}])
    first = allocate_subnets('a', ['default', 'internal'], occupied)
    second = allocate_subnets('a', first, occupied, excluded=first.values())
    assert len(set([*first.values(), *second.values()])) == 4
    for value in [*first.values(), *second.values()]:
        network = ipaddress.ip_network(value)
        assert network.subnet_of(ipaddress.ip_network('10.240.0.0/12'))
        assert network.prefixlen == 24
        assert all(not network.overlaps(used) for used in occupied)


def test_address_space_exhaustion_fails_closed():
    with pytest.raises(RuntimeError, match='refusing automatic Docker IPAM'):
        allocate_subnets('a', ['default'], [ipaddress.ip_network('10.0.0.0/8')])


def test_only_used_project_owned_automatic_bridge_networks_are_selected():
    config = proxy_config()
    extras = {
        'unused': {}, 'external': {'external': True},
        'custom_name': {'name': 'production-network'}, 'overlay': {'driver': 'overlay'},
        'v6only': {'enable_ipv4': False},
        'static': {'ipam': {'config': [{'subnet': '10.5.0.0/24'}]}},
        'plugin': {'ipam': {'driver': 'plugin'}},
    }
    config['networks'].update(extras)
    config['services']['other'] = {'networks': [key for key in extras if key != 'unused']}
    config['networks']['default']['name'] = 'test_default'
    assert set(automatic_networks(config, 'test')) == {'default', 'pier-egress-internal'}
    assert automatic_networks({'services': {'main': {'network_mode': 'none'}}}, 'test') == {}
    assert automatic_networks({'services': {'main': {}}}, 'test') == {'default': {}}


def environment(tmp_path, config):
    env = object.__new__(BitfunDockerEnvironment)
    env.session_id = 'network-test'
    env.trial_paths = SimpleNamespace(trial_dir=tmp_path)
    env._compose_env = Mock(return_value={})
    commands = []
    async def once(command, *args, **kwargs):
        commands.append(command)
        return ExecResult(return_code=0, stdout=json.dumps(config) if command[0] == 'config' else '')
    env._compose_once = once
    return env, commands


@pytest.mark.asyncio
async def test_network_overlap_refreshes_inventory_changes_subnets_and_keeps_policy(tmp_path, monkeypatch):
    import benchmarks.deepswe.docker_environment as module
    config = proxy_config()
    env, commands = environment(tmp_path, config)
    inventory = AsyncMock(return_value=([], []))
    monkeypatch.setattr(module, 'network_inventory', inventory)
    monkeypatch.setattr(module.asyncio, 'sleep', AsyncMock())
    once = env._compose_once
    plans = []
    async def fail_first_two(command, *args, **kwargs):
        result = await once(command, *args, **kwargs)
        if command[0] == 'up':
            plans.append(json.loads(env._network_ipam_path.read_text()))
            if len(plans) < 3:
                return ExecResult(return_code=1, stdout='networks have overlapping IPv4 ' + 'a' * 64)
        return result
    env._compose_once = fail_first_two
    await env._run_docker_compose_command(['up', '--detach', '--wait'])
    assert inventory.await_count == 3
    assert [cmd[0] for cmd in commands] == ['config', 'up', 'down', 'config', 'up', 'down', 'config', 'up']
    subnets = [options['ipam']['config'][0]['subnet']
               for plan in plans for options in plan['networks'].values()]
    assert len(set(subnets)) == 6  # Previously every retry used the same auto-IPAM.
    assert all(set(plan) == {'networks'} for plan in plans)
    assert all(set(options) == {'ipam'} for plan in plans for options in plan['networks'].values())
    assert config['networks']['pier-egress-internal']['internal']
    records = [json.loads(line) for line in (tmp_path / 'docker-lifecycle.jsonl').read_text().splitlines()]
    failures = [record for record in records if record['event'] == 'start_failed']
    assert failures[0]['conflict_network_ids'] == ['a' * 64]
    assert len(records[-1]['subnets']) == 2


@pytest.mark.asyncio
async def test_no_network_grader_never_inspects_or_allocates_host_networks(tmp_path, monkeypatch):
    import benchmarks.deepswe.docker_environment as module
    env, commands = environment(tmp_path, {'services': {'main': {'network_mode': 'none'}}})
    inventory = AsyncMock(side_effect=AssertionError('must not allocate a grader network'))
    monkeypatch.setattr(module, 'network_inventory', inventory)
    await env._run_docker_compose_command(['up', '-d'])
    assert [cmd[0] for cmd in commands] == ['config', 'up']
    assert env._network_ipam_path is None
    inventory.assert_not_called()


@pytest.mark.asyncio
async def test_inventory_failure_prevents_unsafe_start(tmp_path, monkeypatch):
    import benchmarks.deepswe.docker_environment as module
    env, commands = environment(tmp_path, proxy_config())
    monkeypatch.setattr(module, 'network_inventory', AsyncMock(side_effect=RuntimeError('routes unavailable')))
    with pytest.raises(RuntimeError, match='routes unavailable'):
        await env._run_docker_compose_command(['up', '-d'])
    assert [cmd[0] for cmd in commands] == ['config', 'down']


@pytest.mark.asyncio
async def test_repeated_up_reuses_existing_owned_network(tmp_path, monkeypatch):
    import benchmarks.deepswe.docker_environment as module
    env, _ = environment(tmp_path, proxy_config())
    network = {'name': 'network-test_default', 'project': 'network-test',
               'key': 'default', 'driver': 'bridge', 'subnets': ['10.55.0.0/24']}
    monkeypatch.setattr(module, 'network_inventory', AsyncMock(return_value=(
        [ipaddress.ip_network('10.55.0.0/24')], [network])))
    plan = await env._prepare_network_ipam(1, set())
    assert plan['default'] == '10.55.0.0/24'
    assert plan['pier-egress-internal'] != plan['default']
    network['project'] = 'someone-else'
    with pytest.raises(RuntimeError, match='Cannot safely reuse'):
        await env._prepare_network_ipam(1, set())


@pytest.mark.asyncio
async def test_cancellation_cleans_partial_project_without_another_up(tmp_path, monkeypatch):
    import benchmarks.deepswe.docker_environment as module
    env, commands = environment(tmp_path, proxy_config())
    monkeypatch.setattr(module, 'network_inventory', AsyncMock(return_value=([], [])))
    once = env._compose_once
    async def cancel_up(command, *args, **kwargs):
        result = await once(command, *args, **kwargs)
        if command[0] == 'up':
            raise asyncio.CancelledError()
        return result
    env._compose_once = cancel_up
    with pytest.raises(asyncio.CancelledError):
        await env._run_docker_compose_command(['up', '-d'])
    assert [cmd[0] for cmd in commands] == ['config', 'up', 'down']


@pytest.mark.asyncio
async def test_inventory_refreshes_after_unrelated_network_disappears(monkeypatch):
    import benchmarks.deepswe.docker_network as module
    process = AsyncMock(side_effect=[
        (0, '"unix:///var/run/docker.sock"'), (0, 'old'), (1, 'Error: No such network: old'),
        (0, 'new'), (0, json.dumps([{'Id': 'new', 'Name': 'new',
            'IPAM': {'Config': [{'Subnet': '10.0.0.0/16'}]}}])),
        (0, '[{"dst":"default"}]'), (0, '[]')])
    monkeypatch.setattr(module, 'run_compose_process', process)
    occupied, summary = await network_inventory({})
    assert occupied == [ipaddress.ip_network('10.0.0.0/16')]
    assert summary[0]['name'] == 'new'
    assert process.await_count == 7


@pytest.mark.asyncio
@pytest.mark.parametrize('env', [{'DOCKER_HOST': 'ssh://remote'}, {'DOCKER_CONTEXT': 'remote'}])
async def test_remote_daemon_cannot_use_local_routes(monkeypatch, env):
    import benchmarks.deepswe.docker_network as module
    process = AsyncMock(return_value=(0, '"ssh://remote"'))
    monkeypatch.setattr(module, 'run_compose_process', process)
    with pytest.raises(RuntimeError, match='local Unix-socket'):
        await network_inventory(env)
