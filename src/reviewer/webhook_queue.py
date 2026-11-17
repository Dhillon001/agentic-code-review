"""In-process job tracking for the webhook server: delivery dedupe and per-PR latest-wins reviews."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

Runner = Callable[[str, int, str | None], Awaitable[None]]
PRKey = tuple[str, int]


def merge_since(superseded: str | None, new: str | None) -> str | None:
    """`since` for a review that replaces one that never finished.

    The superseded review never posted, so the new one must cover everything *it* would have: keep the
    older base for an incremental pass, and stay a full review if either of them was one."""
    if superseded is None or new is None:
        return None
    return superseded


@dataclass
class _Job:
    since: str | None
    task: asyncio.Task[None]


class DeliveryLog:
    """Bounded memory of `X-GitHub-Delivery` ids, so GitHub redeliveries and retries are handled once."""

    def __init__(self, max_size: int = 2048) -> None:
        self.max_size = max_size
        self._ids: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, delivery_id: object) -> bool:
        return delivery_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, delivery_id: str) -> None:
        self._ids[delivery_id] = None
        self._ids.move_to_end(delivery_id)
        while len(self._ids) > self.max_size:
            self._ids.popitem(last=False)


class ReviewQueue:
    """Runs one review per PR at a time, latest push wins.

    Submitting a review for a PR that already has one queued or running cancels the older job (its head is
    stale), waits for it to unwind, then reviews the new head. A superseded job's `since` is carried over so
    the replacement still covers the commits the cancelled review never posted about."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._jobs: dict[PRKey, _Job] = {}
        self.deliveries = DeliveryLog()

    @property
    def depth(self) -> int:
        """Reviews queued or running."""
        return sum(1 for job in self._jobs.values() if not job.task.done())

    def is_duplicate(self, delivery_id: str | None) -> bool:
        return bool(delivery_id) and delivery_id in self.deliveries

    def mark_delivered(self, delivery_id: str | None) -> None:
        if delivery_id:
            self.deliveries.add(delivery_id)

    def submit(self, repo: str, number: int, since: str | None = None) -> asyncio.Task[None]:
        """Schedule a review on the running event loop; returns its task."""
        key = (repo, number)
        prev = self._jobs.get(key)
        if prev is not None and not prev.task.done():
            prev.task.cancel()
            since = merge_since(prev.since, since)
            log.info("superseding in-flight review of %s#%s", repo, number)
        task = asyncio.get_running_loop().create_task(self._run(key, since, prev.task if prev else None))
        job = _Job(since=since, task=task)
        self._jobs[key] = job
        task.add_done_callback(lambda _t, key=key, job=job: self._forget(key, job))
        return task

    async def _run(self, key: PRKey, since: str | None, prev: asyncio.Task[None] | None) -> None:
        if prev is not None and not prev.done():
            # Let the superseded review unwind (a review being posted finishes posting) before starting.
            await asyncio.wait({prev})
        repo, number = key
        try:
            await self._runner(repo, number, since)
        except asyncio.CancelledError:
            log.info("review of %s#%s cancelled: a newer push superseded it", repo, number)
            raise

    def _forget(self, key: PRKey, job: _Job) -> None:
        if self._jobs.get(key) is job:
            del self._jobs[key]

    async def drain(self) -> None:
        """Wait until no review is queued or running (used by tests and on shutdown)."""
        while tasks := [job.task for job in self._jobs.values() if not job.task.done()]:
            await asyncio.wait(tasks)
