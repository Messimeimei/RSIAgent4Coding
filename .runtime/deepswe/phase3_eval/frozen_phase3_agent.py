"""Pier agent: baseline Phase-3 eval with a pre-frozen host memory bank.

Runtime-only helper. Does not modify repository source. Mirrors BitfunRSI's
phase3 steps: upload frozen memory into the pristine Pier target container,
run MemoryBaseline(training=False), then assert the host freeze is unchanged.
Official Pier grading still runs after the agent returns.
"""

from __future__ import annotations

import base64
import io
import tarfile
from pathlib import Path

from benchmarks.deepswe.contracts import RoleInfrastructureError
from benchmarks.deepswe.rsi import MemoryBaseline
from explore.practice_loop import _read_memory_tree
from explore.target_learning import _memory_tree_sha256


_CHUNK = 3000


class FrozenMemoryPhase3(MemoryBaseline):
    def __init__(self, *args, frozen_memory_path=None, **kwargs):
        if not frozen_memory_path:
            raise ValueError("frozen_memory_path is required")
        path = Path(frozen_memory_path).resolve()
        if not path.is_dir():
            raise ValueError(f"frozen memory directory missing: {path}")
        memory = _read_memory_tree(str(path))
        if not memory:
            raise ValueError(f"frozen memory directory is empty: {path}")
        kwargs.pop("training", None)
        super().__init__(*args, memory=memory, training=False, **kwargs)
        self.frozen_memory_path = path
        self.frozen_memory_sha256 = _memory_tree_sha256(memory)
        self.phase3_meta = {
            "frozen_memory_path": str(path),
            "frozen_memory_files": len(memory),
            "frozen_memory_bytes": sum(map(len, memory.values())),
            "frozen_memory_sha256": self.frozen_memory_sha256,
        }

    @staticmethod
    def name():
        return "bitfun-phase3-frozen"

    async def _push_frozen_memory(self, environment) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            # tarfile.add recurses into directories. Filtering to is_file()
            # silently drops a layered memory_frozen (environment/, patterns/,
            # practices/), matching explore/commit.py:push_memory semantics.
            for name in sorted(p.name for p in self.frozen_memory_path.iterdir()):
                archive.add(self.frozen_memory_path / name, arcname=name)
        payload = base64.b64encode(buf.getvalue()).decode("ascii")

        async def run(command: str, timeout_sec: int = 120) -> str:
            result = await environment.exec(command=command, timeout_sec=timeout_sec)
            return (result.stdout or "") + (result.stderr or "")

        await run("rm -rf /home/user/.memory /tmp/mem_in.b64 && mkdir -p /home/user/.memory")
        for index in range(0, len(payload), _CHUNK):
            chunk = payload[index : index + _CHUNK]
            await run(f"printf %s '{chunk}' >> /tmp/mem_in.b64", timeout_sec=30)
        output = await run(
            "base64 -d /tmp/mem_in.b64 > /tmp/mem_in.tgz 2>/tmp/mem_err && "
            "tar xzf /tmp/mem_in.tgz -C /home/user/.memory 2>>/tmp/mem_err; "
            "echo SYNC_RC=$?; "
            "echo MEM_FILES=$(find /home/user/.memory -type f | wc -l)"
        )
        if "SYNC_RC=0" not in output:
            raise RoleInfrastructureError(
                "Frozen evaluation memory upload failed: " + output[-500:]
            )
        expected = len(_read_memory_tree(str(self.frozen_memory_path)))
        delivered = None
        for line in output.splitlines():
            if line.startswith("MEM_FILES="):
                try:
                    delivered = int(line.split("=", 1)[1].strip())
                except ValueError:
                    delivered = None
        if delivered != expected:
            raise RoleInfrastructureError(
                "Frozen memory upload incomplete: container reports "
                f"{delivered} files, host bank has {expected}"
            )

    async def run(self, instruction, environment, context):
        await self._push_frozen_memory(environment)
        try:
            await super().run(instruction, environment, context)
        finally:
            current = _memory_tree_sha256(_read_memory_tree(str(self.frozen_memory_path)))
            unchanged = current == self.frozen_memory_sha256
            self.phase3_meta["frozen_memory_unchanged"] = unchanged
            if hasattr(self, "summary") and isinstance(self.summary, dict):
                self.summary["phase3"] = dict(self.phase3_meta)
            if not unchanged:
                raise RoleInfrastructureError("Phase3 changed frozen host memory")

    def populate_context_post_run(self, context):
        super().populate_context_post_run(context)
        context.metadata = context.metadata or {}
        context.metadata["phase3"] = dict(self.phase3_meta)
