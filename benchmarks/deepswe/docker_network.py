"""Explicit, host-aware IPAM for private RSI Compose networks.

Call inventory/allocation while holding network_lifecycle_lock through compose up.
Docker remains the final arbiter against clients which do not share our lock.
Never modify daemon address pools or remove another project's networks.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json

from .docker_lifecycle import run_compose_process


def ipv4_network(value):
    network = ipaddress.ip_network(value, strict=False)
    return network if network.version == 4 else None


def occupied_subnets(networks, routes, interfaces):
    occupied = set()
    for record in networks:
        for entry in record.get('IPAM', {}).get('Config') or []:
            if entry.get('Subnet'):
                occupied.add(ipv4_network(entry['Subnet']))
    for route in routes:
        destination = route.get('dst', 'default')
        if destination not in ('default', '0.0.0.0/0'):
            occupied.add(ipv4_network(destination))
    for interface in interfaces:
        for address in interface.get('addr_info', []):
            if address.get('family') == 'inet':
                occupied.add(ipv4_network(
                    f"{address['local']}/{address['prefixlen']}"))
    occupied.discard(None)
    return sorted(occupied, key=lambda net: (int(net.network_address), net.prefixlen))


def automatic_networks(config, project):
    """Select used, project-owned bridge networks without explicit task IPAM."""
    used = set()
    for service in config.get('services', {}).values():
        if not service.get('network_mode'):
            used.update(service.get('networks') or ['default'])
    selected = {}
    for key in sorted(used):
        network = config.get('networks', {}).get(key) or {}
        ipam = network.get('ipam') or {}
        if (network.get('external') or network.get('driver', 'bridge') != 'bridge'
                or network.get('enable_ipv4') is False
                or ipam.get('config') or ipam.get('driver', 'default') != 'default'
                or network.get('name', f'{project}_{key}') != f'{project}_{key}'):
            continue
        selected[key] = network
    return selected


def allocate_subnets(project, keys, occupied, *, excluded=()):
    """Choose separate /24s (253 container addresses each) in private 10/8."""
    pool = ipaddress.ip_network('10.0.0.0/8')
    count = 1 << (24 - pool.prefixlen)
    first = int(pool.network_address)
    blocked = bytearray(count)
    for network in [*occupied, *(ipv4_network(item) for item in excluded)]:
        if network is None or not network.overlaps(pool):
            continue
        start = max(0, (int(network.network_address) - first) // 256)
        end = min(count, (int(network.broadcast_address) - first) // 256 + 1)
        blocked[start:end] = b'\x01' * (end - start)
    offset = int.from_bytes(hashlib.sha256(project.encode()).digest()[:4], 'big') % count
    available = (ipaddress.ip_network((first + ((offset + i) % count) * 256, 24))
                 for i in range(count) if not blocked[(offset + i) % count])
    selected = {}
    for key in sorted(keys):
        subnet = next(available, None)
        if subnet is None:
            raise RuntimeError('No safe RSI /24 subnet remains in 10.0.0.0/8; '
                               'refusing automatic Docker IPAM fallback')
        selected[key] = str(subnet)
    return selected


async def network_inventory(env):
    """Fail closed if local routes or Docker inventory cannot be inspected."""
    async def command(args):
        code, output = await run_compose_process(args, env, 30, stdout_only=True)
        if code:
            # Commands contain no credentials, but daemon output may do so.
            raise RuntimeError(f'RSI network inventory failed: {args[:3]} (exit {code})')
        return output

    if env.get('DOCKER_HOST') and not env.get('DOCKER_CONTEXT'):
        endpoint = env['DOCKER_HOST']
    else:
        endpoint = json.loads(await command([
            'docker', 'context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}']))
    if not isinstance(endpoint, str) or not endpoint.startswith('unix://'):
        raise RuntimeError('RSI safe IPAM requires a local Unix-socket Docker daemon; '
                           'remote host routes cannot be verified')
    # Other Docker clients may delete a network between ls and inspect. Refresh
    # the entire inventory, never silently omit an uninspected network.
    for attempt in range(3):
        ids = (await command(['docker', 'network', 'ls', '--quiet', '--no-trunc'])).split()
        networks = []
        vanished = False
        for start in range(0, len(ids), 100):
            code, output = await run_compose_process(
                ['docker', 'network', 'inspect', *ids[start:start + 100]], env, 30,
                stdout_only=True)
            if code:
                if 'no such network' in output.lower() and attempt < 2:
                    vanished = True
                    break
                raise RuntimeError('RSI Docker network inspection failed; refusing unsafe allocation')
            networks.extend(json.loads(output))
        if not vanished:
            break
    routes = json.loads(await command(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
    interfaces = json.loads(await command(['ip', '-j', '-4', 'address', 'show']))
    if not all(isinstance(data, list) for data in (networks, routes, interfaces)):
        raise RuntimeError('Malformed RSI network inventory')
    occupied = occupied_subnets(networks, routes, interfaces)
    # Record network metadata only, never container environments or proxy tokens.
    summary = [{
        'id': n['Id'], 'name': n['Name'], 'driver': n.get('Driver'),
        'internal': n.get('Internal', False),
        'project': (n.get('Labels') or {}).get('com.docker.compose.project'),
        'key': (n.get('Labels') or {}).get('com.docker.compose.network'),
        'subnets': [c['Subnet'] for c in n.get('IPAM', {}).get('Config') or [] if c.get('Subnet')],
    } for n in networks]
    return occupied, summary
