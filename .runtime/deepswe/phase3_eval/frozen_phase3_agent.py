"""Pier agent: baseline Phase-3 eval with a pre-frozen host memory bank.

Runtime-only helper. Does not modify repository source. Mirrors BitfunRSI's
phase3 steps: upload frozen memory into the pristine Pier target container,
run MemoryBaseline(training=False), then assert the host freeze is unchanged.
Official Pier grading still runs after the agent returns.
"""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import tarfile
from pathlib import Path

from benchmarks.deepswe.contracts import RoleInfrastructureError
from benchmarks.deepswe.rsi import MemoryBaseline
from explore.practice_loop import _read_memory_tree
from explore.target_learning import _memory_tree_sha256


class FrozenMemoryPhase3(MemoryBaseline):
    def __init__(self, *args, frozen_memory_path=None, source_memory_path=None,
                 memory_provenance=None, **kwargs):
        if memory_provenance is not None:
            raise ValueError("Memory provenance is host-only metadata; extra Actor prompt content is forbidden")
        if not frozen_memory_path:
            raise ValueError("frozen_memory_path is required")
        path = Path(frozen_memory_path).resolve()
        if not path.is_dir():
            raise ValueError(f"frozen memory directory missing: {path}")
        memory = _read_memory_tree(str(path))
        self.source_memory_path = Path(source_memory_path).resolve() if source_memory_path else None
        if self.source_memory_path is not None:
            if not self.source_memory_path.is_dir() or _read_memory_tree(str(self.source_memory_path)) != memory:
                raise ValueError("Frozen memory must match the source paths and bytes exactly")
        kwargs.pop("training", None)
        super().__init__(*args, memory=memory, training=False, **kwargs)
        self.frozen_memory_path = path
        self.frozen_memory_sha256 = _memory_tree_sha256(memory)
        self.phase3_meta = {
            "frozen_memory_path": str(path),
            "frozen_memory_files": len(memory),
            "frozen_memory_bytes": sum(map(len, memory.values())),
            "frozen_memory_sha256": self.frozen_memory_sha256,
            "source_memory_path": str(self.source_memory_path) if self.source_memory_path else None,
            "memory_input_policy": "original_frozen_files_only",
            "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }

    @staticmethod
    def name():
        return "bitfun-phase3-frozen"

    async def _push_frozen_memory(self, environment) -> None:
        if _memory_tree_sha256(_read_memory_tree(str(self.frozen_memory_path))) != self.frozen_memory_sha256:
            raise RoleInfrastructureError("Frozen memory changed before upload")
        if self.source_memory_path is not None and _read_memory_tree(str(self.source_memory_path)) != self.memory:
            raise RoleInfrastructureError("Original memory source changed before upload")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        local = self.logs_dir / "frozen-memory.tgz"
        with tarfile.open(local, mode="w:gz") as archive:
            for name, content in sorted(self.memory.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(content), 0o644
                archive.addfile(info, io.BytesIO(content))
        remote = "/tmp/rsi-phase3-memory.tgz"
        await environment.upload_file(local, remote)
        expected = {name: hashlib.sha256(content).hexdigest() for name, content in self.memory.items()}
        script = """import hashlib,json,pathlib,shutil,sys,tarfile
root=pathlib.Path('/home/user/.memory')
if root.is_symlink(): raise RuntimeError('Memory root must not be a symlink')
if root.exists(): shutil.rmtree(root)
root.mkdir(parents=True)
with tarfile.open('/tmp/rsi-phase3-memory.tgz') as archive:
    archive.extractall(root, filter='data')
actual={}
for path in root.rglob('*'):
    if path.is_symlink(): raise RuntimeError('Unexpected memory symlink')
    if path.is_file(): actual[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
if actual != json.loads(sys.argv[1]): raise RuntimeError('Frozen memory file/hash mismatch')
print(json.dumps({'verified':True,'files':len(actual)}))
"""
        result = await environment.exec(
            command=shlex.join(["python3", "-c", script, json.dumps(expected)]), timeout_sec=120)
        if result.return_code:
            raise RoleInfrastructureError("Frozen evaluation memory upload/hash verification failed")
        self.phase3_meta["delivery"] = json.loads(result.stdout)

    async def run(self, instruction, environment, context):
        await self._push_frozen_memory(environment)
        try:
            await super().run(instruction, environment, context)
        finally:
            current = _memory_tree_sha256(_read_memory_tree(str(self.frozen_memory_path)))
            unchanged = current == self.frozen_memory_sha256
            if self.source_memory_path is not None:
                unchanged = unchanged and _read_memory_tree(str(self.source_memory_path)) == self.memory
            self.phase3_meta["frozen_memory_unchanged"] = unchanged
            if hasattr(self, "summary") and isinstance(self.summary, dict):
                self.summary["phase3"] = dict(self.phase3_meta)
                self._flush()
            (self.logs_dir.parent / "phase3-memory.json").write_text(
                json.dumps(self.phase3_meta, indent=2) + "\n")
            if not unchanged:
                raise RoleInfrastructureError("Phase3 changed frozen host memory")

    def populate_context_post_run(self, context):
        super().populate_context_post_run(context)
        context.metadata = context.metadata or {}
        context.metadata["phase3"] = dict(self.phase3_meta)
