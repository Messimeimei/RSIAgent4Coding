"""Exercise the real shell/exec boundary without Docker or a model service."""
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.deepswe.bitfun_agent import BitfunCli
from benchmarks.deepswe.session import BitfunSession


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "new_session", "resume"])
async def test_large_prompt_reaches_stdin_exactly_without_shell_expansion(tmp_path, mode):
    marker = tmp_path / "MUST_NOT_EXIST"
    payload = (f"引号 ' \" `touch {marker}` $(touch {marker})\\n\n" * 1600 + "END\n").encode()
    assert len(payload) > 131072  # Linux MAX_ARG_STRLEN, independently of total ARG_MAX
    received = tmp_path / "received.json"
    binary = tmp_path / "bitfun-stub"
    binary.write_text(f"#!{sys.executable}\nimport sys,json,hashlib\nfrom pathlib import Path\n"
        f"Path({str(received)!r}).write_text(json.dumps({{'argv':sys.argv[1:],"
        "'sha256':hashlib.sha256(sys.stdin.buffer.read()).hexdigest()}))\n")
    binary.chmod(0o700)
    cls = BitfunCli if mode == "native" else BitfunSession
    agent = cls(logs_dir=tmp_path / "logs", binary_path=str(binary), output_patch_path=None,
                bitfun_config={"ai": {"models": []}})
    uploads = []

    async def upload_file(source, remote):
        uploads.append((Path(source).read_bytes(), remote))

    remote = await agent.upload_prompt(SimpleNamespace(upload_file=upload_file),
                                      payload.decode(), "prompt.txt")
    assert uploads == [(payload, remote)]
    assert (agent.logs_dir / "prompt.txt").stat().st_mode & 0o777 == 0o600
    if mode == "resume":
        agent.calls = 1
    command = (agent._build_run_shell(remote) if mode == "native" else agent.command(remote))
    # Map guest paths to the local sandbox; retain the actual two shell layers,
    # process-group wrapper, pipe/tee and exit-code propagation.
    guest_tmp = tmp_path / "guest"
    guest_tmp.mkdir()
    (guest_tmp / "prompt.txt").write_bytes(payload)
    command = command.replace("/tmp/rsiagent-bitfun", str(guest_tmp)).replace("/logs/agent", str(agent.logs_dir))
    assert len(command.encode()) < 8192 and "MUST_NOT_EXIST" not in command
    result = subprocess.run(["bash", "-c", "bash -c " + shlex.quote(command)],
                            capture_output=True, timeout=15, env=dict(os.environ))
    assert result.returncode == 0, result.stderr.decode()
    actual = json.loads(received.read_text())
    assert actual["sha256"] == hashlib.sha256(payload).hexdigest()
    if mode != "native":
        flag = "--resume" if mode == "resume" else "--session-id"
        assert actual["argv"][actual["argv"].index(flag) + 1] == agent.session_id
    assert not marker.exists()


@pytest.mark.asyncio
async def test_failed_prompt_upload_does_not_launch_session(tmp_path):
    from unittest.mock import AsyncMock
    agent = BitfunSession(logs_dir=tmp_path, bitfun_config={"ai": {"models": []}})
    agent.exec_as_agent = AsyncMock()
    env = SimpleNamespace(upload_file=AsyncMock(side_effect=OSError("upload interrupted")))
    with pytest.raises(OSError, match="upload interrupted"):
        await agent.execute(env, "task", 10, role="actor", handoff_id="h")
    agent.exec_as_agent.assert_not_awaited()
    assert agent.calls == 0
