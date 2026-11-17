import asyncio

import pytest

from reviewer.webhook_queue import DeliveryLog, ReviewQueue, merge_since


class Recorder:
    """Runner whose reviews block until released, recording starts, finishes and cancellations."""

    def __init__(self):
        self.started: list[tuple] = []
        self.finished: list[tuple] = []
        self.cancelled: list[tuple] = []
        self.gates: dict[tuple, asyncio.Event] = {}

    def gate(self, repo, number, since) -> asyncio.Event:
        return self.gates.setdefault((repo, number, since), asyncio.Event())

    async def __call__(self, repo, number, since):
        job = (repo, number, since)
        self.started.append(job)
        try:
            await self.gate(*job).wait()
        except asyncio.CancelledError:
            self.cancelled.append(job)
            raise
        self.finished.append(job)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("superseded", "new", "expected"),
    [("a", "b", "a"), (None, "b", None), ("a", None, None), (None, None, None)],
)
def test_merge_since(superseded, new, expected):
    assert merge_since(superseded, new) == expected


async def test_new_push_cancels_in_flight_review_and_keeps_older_base():
    run = Recorder()
    q = ReviewQueue(run)
    q.submit("o/r", 1, "base0")
    await _settle()
    assert run.started == [("o/r", 1, "base0")] and q.depth == 1

    newer = q.submit("o/r", 1, "head1")
    await _settle()
    assert run.cancelled == [("o/r", 1, "base0")]
    # The cancelled review never posted, so the replacement still covers everything since base0.
    assert run.started[-1] == ("o/r", 1, "base0") and len(run.started) == 2
    assert q.depth == 1

    run.gate("o/r", 1, "base0").set()
    await newer
    assert run.finished == [("o/r", 1, "base0")]
    assert q.depth == 0


async def test_superseding_a_full_review_stays_full():
    run = Recorder()
    q = ReviewQueue(run)
    q.submit("o/r", 1, None)  # e.g. `opened`
    await _settle()
    q.submit("o/r", 1, "head1")  # a push before the first review finished
    await _settle()
    assert run.started == [("o/r", 1, None), ("o/r", 1, None)]
    run.gate("o/r", 1, None).set()
    await q.drain()


async def test_queued_review_superseded_before_it_starts():
    run = Recorder()
    q = ReviewQueue(run)
    q.submit("o/r", 1, "a")
    q.submit("o/r", 1, "b")  # same tick: the first never reaches the runner
    run.gate("o/r", 1, "a").set()
    await q.drain()
    assert run.started == [("o/r", 1, "a")] and run.cancelled == []


async def test_finished_review_is_not_superseded():
    run = Recorder()
    q = ReviewQueue(run)
    run.gate("o/r", 1, "a").set()
    await q.submit("o/r", 1, "a")
    run.gate("o/r", 1, "b").set()
    await q.submit("o/r", 1, "b")
    assert run.finished == [("o/r", 1, "a"), ("o/r", 1, "b")] and run.cancelled == []


async def test_different_prs_run_concurrently():
    run = Recorder()
    q = ReviewQueue(run)
    q.submit("o/r", 1, None)
    q.submit("o/r", 2, None)
    q.submit("o/other", 1, None)
    await _settle()
    assert len(run.started) == 3 and run.cancelled == [] and q.depth == 3
    for g in list(run.gates.values()):
        g.set()
    await q.drain()
    assert q.depth == 0


async def test_new_review_waits_for_superseded_one_to_unwind():
    order = []
    release = asyncio.Event()

    async def runner(repo, number, since):
        order.append(("start", since))
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()  # e.g. a shielded post finishing up
            order.append(("unwound", since))
            raise

    q = ReviewQueue(runner)
    q.submit("o/r", 1, "a")
    await _settle()
    q.submit("o/r", 1, "b")
    await _settle()
    assert order == [("start", "a")]
    release.set()
    await _settle()
    assert order == [("start", "a"), ("unwound", "a"), ("start", "a")]
    q._jobs[("o/r", 1)].task.cancel()
    await q.drain()


async def test_failing_review_leaves_queue_usable():
    async def boom(repo, number, since):
        raise RuntimeError("x")

    q = ReviewQueue(boom)
    task = q.submit("o/r", 1, None)
    with pytest.raises(RuntimeError):
        await task
    assert q.depth == 0


def test_delivery_log_is_bounded_and_forgets_oldest():
    log = DeliveryLog(max_size=2)
    for d in ("a", "b", "c"):
        log.add(d)
    assert "a" not in log and "b" in log and "c" in log and len(log) == 2


def test_duplicate_check_ignores_missing_ids():
    q = ReviewQueue(lambda *a: None)
    q.mark_delivered(None)
    q.mark_delivered("")
    assert not q.is_duplicate(None) and not q.is_duplicate("")
    q.mark_delivered("d")
    assert q.is_duplicate("d")
