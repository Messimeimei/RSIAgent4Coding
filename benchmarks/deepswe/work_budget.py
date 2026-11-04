"""Host-only working clock, with explicit model-transport pause records."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


class WorkBudget:
    def __init__(self, seconds, *, clock=time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self.started = clock()
        self.paths = {}
        self.requests = {}
        self.sources = {}
        self.intervals = []
        self.pauses = 0
        self.settled = 0
        self.setup_seconds = 0.0
        self.setup_started = None

    @contextmanager
    def initial_setup(self):
        """Initial machine/session preparation precedes the reference work loop."""
        self.setup_started = self.clock()
        try:
            yield
        finally:
            self.setup_seconds += self.clock() - self.setup_started
            self.setup_started = None

    @property
    def setup_elapsed(self):
        return self.setup_seconds + (
            self.clock() - self.setup_started if self.setup_started is not None else 0.0
        )

    def watch(self, path):
        self.paths.setdefault(Path(path), (0, ""))

    def poll(self):
        for path, (offset, pending) in list(self.paths.items()):
            if not path.exists():
                continue
            with path.open() as stream:
                stream.seek(offset)
                lines = (pending + stream.read()).split("\n")
                self.paths[path] = (stream.tell(), lines.pop())
            for line in lines:
                record = json.loads(line)
                if record["event"] == "request_start":
                    self.sources[record["request_id"]] = path
                self.feed(record)

    def finish(self, path):
        """The invocation's process group has settled; no request remains live."""
        self.poll()
        for key in list(self.requests):
            if self.sources.get(key) == Path(path):
                self.feed(dict(request_id=key, event="request_end", at=self.clock()))
        self.paths.pop(Path(path), None)

    def feed(self, event):
        key = event["request_id"]
        kind = event["event"]
        if kind == "request_start":
            self.requests[key] = None
        elif kind == "pause_start":
            self.requests[key] = event["started"]
            self.pauses += 1
        elif kind == "pause_end":
            start = self.requests.get(key)
            if start is not None:
                self.intervals.append((start, event["at"]))
            self.requests[key] = None
        elif kind == "request_end":
            start = self.requests.pop(key, None)
            if start is not None:
                self.intervals.append((start, event["at"]))
            self.sources.pop(key, None)
            self.settled += 1

    @property
    def paused_seconds(self):
        now = self.clock()
        intervals = self.intervals + [
            (start, now) for start in self.requests.values() if start is not None
        ]
        # Requests can overlap (native subagents). Never credit the same second
        # twice, including repeated polls while an outage is still in progress.
        end = self.started
        total = 0.0
        for start, stop in sorted(intervals):
            start, stop = max(start, end, self.started), min(stop, now)
            if stop > start:
                total += stop - start
                end = stop
        return total

    @property
    def elapsed(self):
        return max(
            0.0, self.clock() - self.started - self.paused_seconds - self.setup_elapsed
        )

    def remaining(self):
        self.poll()
        return self.seconds - self.elapsed

    @asynccontextmanager
    async def limit(self, seconds=None):
        """Check the working ceiling as explicit infrastructure pauses accrue.

        The reference checks its ceiling between model operations. Let an
        in-flight request settle before deciding whether its duration was work
        or a failed transport attempt. The relay bounds each network read.
        """
        cap = self.seconds if seconds is None else seconds
        async with asyncio.timeout(None) as timeout:

            async def monitor():
                observed_settled = self.settled
                while True:
                    self.poll()
                    # Decide AFTER polling instead of installing a fixed future
                    # deadline that could fire before the next request-start poll.
                    at_boundary = not self.requests or self.settled != observed_settled
                    observed_settled = self.settled
                    if at_boundary and self.elapsed >= cap:
                        timeout.reschedule(asyncio.get_running_loop().time())
                        return
                    await asyncio.sleep(0.05)

            watcher = asyncio.create_task(monitor())
            watcher.add_done_callback(
                lambda task: (
                    timeout.reschedule(asyncio.get_running_loop().time())
                    if not task.cancelled() and task.exception() is not None
                    else None
                )
            )
            try:
                yield timeout
            finally:
                watcher.cancel()
                result = (await asyncio.gather(watcher, return_exceptions=True))[0]
                if isinstance(result, Exception):
                    raise result
                self.poll()

    def summary(self):
        self.poll()
        return {
            "work_elapsed_sec": round(self.elapsed, 3),
            "infra_pause_sec": round(self.paused_seconds, 3),
            "infra_pauses": self.pauses,
            "initial_setup_sec": round(self.setup_elapsed, 3),
        }
