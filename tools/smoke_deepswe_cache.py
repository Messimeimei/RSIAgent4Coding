#!/usr/bin/env python3
"""Run inside a disposable Koota task container; no model or network required."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.deepswe.rsi_integrity import (
    VERIFIER_CACHE_ENV, inventory, prepare_working_copy, recover, verify_working_copy,
)


def main():
    vitest = Path('/app/packages/core/node_modules/vitest').resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='rsi-cache-smoke-') as temporary:
        root = Path(temporary) / 'original'
        (root / 'node_modules').mkdir(parents=True)
        (root / 'node_modules/vitest').symlink_to(vitest)
        (root / 'package.json').write_text('{"type":"module"}')
        (root / 'answer.test.js').write_text(
            "import { test, expect } from 'vitest';\n"
            "test('answer', () => { expect(6 * 7).toBe(42) });\n")

        def run(where, environment=None):
            result = subprocess.run(['node', str(vitest / 'vitest.mjs'), 'run',
                '--root', str(where), '--maxWorkers', '1'], cwd=where,
                env={**os.environ, **(environment or {})}, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)

        run(root)
        caches = list(root.glob('node_modules/.vite/vitest/**/results.json'))
        assert caches, 'Real Vitest did not create its results cache'
        # Make the subsequent real test duration observably different.
        for cache in caches:
            value = json.loads(cache.read_text())
            for _, result in value['results']:
                result['duration'] = 123456
            cache.write_text(json.dumps(value))
        before = inventory([str(root)])
        destination = Path(temporary) / 'scratch/candidate'
        workspace = prepare_working_copy(before, destination)
        cache_env = {key: value.replace('/tmp/verifier-scratch', str(destination.parent))
                     for key, value in VERIFIER_CACHE_ENV.items()}
        run(destination, cache_env)
        result = verify_working_copy(workspace)
        assert result['status'] == 'verified', result
        assert result['result_caches'], 'Expected an existing cache to be updated'
        assert inventory([str(root)]) == before
        assert recover(before)['status'] == 'unchanged'
        (destination / 'answer.test.js').write_text('throw new Error("modified source")')
        changed = verify_working_copy(workspace)
        assert changed['status'] == 'changed' and 'answer.test.js' in changed['changed']
        assert inventory([str(root)]) == before
        print(json.dumps({'status': 'passed', 'real_vitest': True,
            'cache_updates': result['result_caches'], 'original_unchanged': True,
            'workspace_edit_recorded': True, 'model_calls': 0}, indent=2))


if __name__ == '__main__':
    main()
