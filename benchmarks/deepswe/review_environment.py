"""Committed candidate snapshots and disposable, private review environments."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shlex
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from pier.models.trial.paths import TrialPaths
from pier.models.agent.network import NetworkAllowlist

from .docker_environment import BitfunDockerEnvironment


@dataclass(frozen=True)
class Candidate:
    base_commit: str
    head: str
    repo: str
    status: str
    patch_sha256: str
    patch_path: Path

    @property
    def candidate_id(self):
        return f"{self.head}:{self.patch_sha256}"

    def to_dict(self):
        return {
            **asdict(self),
            "patch_path": str(self.patch_path),
            "candidate_id": self.candidate_id,
        }


async def repository_state(environment):
    script = """import json, subprocess
def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()
print(json.dumps({'repo': git('rev-parse', '--show-toplevel'),
                  'head': git('rev-parse', 'HEAD'),
                  'status': git('status', '--porcelain=v1', '--untracked-files=all')}))
"""
    result = await environment.exec(command="python3 -c " + shlex.quote(script))
    if result.return_code:
        raise RuntimeError("Cannot inspect the candidate repository")
    return json.loads(result.stdout)


async def capture_candidate(environment, base_commit, destination):
    state = await repository_state(environment)
    if not state["repo"].startswith("/"):
        raise ValueError("Expected an absolute repository path")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    remote_patch = f"/tmp/rsiagent-bitfun/candidate-{uuid.uuid4().hex}.patch"
    result = await environment.exec(
        command=shlex.join(["git", "diff", "--binary", base_commit, state["head"]])
        + " > "
        + shlex.quote(remote_patch),
        cwd=state["repo"],
    )
    if result.return_code:
        raise RuntimeError("Cannot export the committed candidate patch")
    patch = destination / "candidate.patch"
    await environment.download_file(remote_patch, patch)
    candidate = Candidate(
        base_commit=base_commit,
        patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest(),
        patch_path=patch,
        **state,
    )
    (destination / "candidate.json").write_text(
        json.dumps(candidate.to_dict(), indent=2) + "\n"
    )
    return candidate


async def review_tree_digest(environment, base_commit):
    script = (
        "import hashlib,subprocess; "
        f"data=subprocess.check_output(['git','diff','--binary','--no-ext-diff','--no-textconv',{base_commit!r}]); "
        "print(hashlib.sha256(data).hexdigest())"
    )
    result = await environment.exec(command="python3 -c " + shlex.quote(script))
    if result.return_code:
        raise RuntimeError("Cannot fingerprint the review candidate")
    return result.stdout.strip()


async def repository_fingerprint(environment):
    state = await repository_state(environment)
    return (
        state["head"],
        state["status"],
        await review_tree_digest(environment, state["head"]),
    )


def make_review_environment(
    actor_environment, reviewer, destination, private_root=None
):
    paths = TrialPaths(Path(destination))
    # Only immutable tool binaries cross this boundary. Actor logs, sessions,
    # repository mounts, and official grader assets are deliberately not shared.
    required = (
        {reviewer._binary_path, "/usr/local/bin/uv"}
        if reviewer
        else {"/usr/local/bin/uv"}
    )
    mounts = [
        copy.deepcopy(mount)
        for mount in actor_environment._mounts_json
        if mount.get("type") == "bind"
        and mount.get("target") in required
        and mount.get("read_only") is True
    ]
    if {mount["target"] for mount in mounts} != required:
        raise ValueError("Review requires the read-only BitFun and uv binary mounts")
    if private_root is not None:
        if reviewer is None:
            raise ValueError("Completion probes must not mount Verifier private state")
        for name, target in [
            ("home", "/tmp/rsiagent-bitfun/home"),
            ("scratch", "/tmp/verifier-scratch"),
        ]:
            source = Path(private_root).resolve() / name
            source.mkdir(parents=True, exist_ok=True, mode=0o700)
            mounts.append(
                {
                    "type": "bind",
                    "source": str(source),
                    "target": target,
                    "read_only": False,
                    "bind": {"create_host_path": False},
                }
            )
    # Explicitly mount only this disposable environment's logs. Completion probes
    # have neither a model proxy nor any role's credentials or conversation.
    mounts.append(
        {
            "type": "bind",
            "source": str(paths.agent_dir.resolve()),
            "target": "/logs/agent",
            "read_only": False,
        }
    )
    return BitfunDockerEnvironment(
        environment_dir=actor_environment.environment_dir,
        environment_name=actor_environment.environment_name,
        session_id=f"baseline-review-{uuid.uuid4().hex[:16]}",
        trial_paths=paths,
        task_env_config=actor_environment.task_env_config.model_copy(deep=True),
        network_allowlist=reviewer.network_allowlist()
        if reviewer
        else NetworkAllowlist(),
        default_user=actor_environment.default_user,
        cpu_enforcement_policy=actor_environment._cpu_resource_mode,
        memory_enforcement_policy=actor_environment._memory_resource_mode,
        inference_ports=actor_environment._inference_ports,
        mounts_json=mounts,
        logger=reviewer.logger if reviewer else actor_environment.logger,
    )


@asynccontextmanager
async def review_environment(
    actor_environment, reviewer, candidate, destination, private_root=None
):
    environment = make_review_environment(
        actor_environment, reviewer, destination, private_root
    )
    environment.trial_paths.mkdir()
    try:
        await environment.start(force_build=False)
        state = await repository_state(environment)
        if (
            state["head"] != candidate.base_commit
            or state["repo"] != candidate.repo
            or state["status"]
        ):
            raise RuntimeError(
                "Review environment does not match the pristine task base"
            )
        await environment.upload_file(
            candidate.patch_path, "/tmp/baseline-candidate.patch"
        )
        result = await environment.exec(
            command="if test -s /tmp/baseline-candidate.patch; then "
            "git apply --check /tmp/baseline-candidate.patch && "
            "git apply --index /tmp/baseline-candidate.patch; fi",
            cwd=candidate.repo,
        )
        if result.return_code:
            raise RuntimeError(
                "Could not apply candidate in isolated review environment"
            )
        yield environment
    finally:
        # Keep the shared task image while the Actor still owns its container.
        # All removals are scoped to this unique review compose project.
        try:
            await asyncio.wait_for(environment.prepare_logs_for_host(), timeout=20)
            await asyncio.wait_for(
                environment._run_docker_compose_command(
                    ["down", "--rmi", "local", "--volumes", "--remove-orphans"]
                ),
                timeout=90,
            )
        finally:
            environment._cleanup_resources_compose_file()
