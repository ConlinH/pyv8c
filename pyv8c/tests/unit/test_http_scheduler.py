import asyncio
import time
from concurrent.futures import Future

import pytest

from pyv8c.foundation.host_errors import HostStateError
from pyv8c.network.http_scheduler import HttpScheduler
from pyv8c.network.models import NetworkRequest


class Tasks:
    def __init__(self):
        self.pending = set()

    def begin_network(self, stop):
        self.pending.add(stop)

    def complete_network(self, stop):
        self.pending.remove(stop)


async def test_round_robin_gives_a_waiting_page_a_slot_before_returning_to_the_busy_page():
    sent = {}
    b_started = asyncio.Event()

    def start(job):
        request = job.request
        sent[request.url] = Future()
        if request.url.endswith("/b1"):
            b_started.set()
        return sent[request.url]

    scheduler = HttpScheduler(start, limit=1, navigation_slots=0)
    a, b = Tasks(), Tasks()
    jobs = [scheduler.submit(tasks, tasks, NetworkRequest(f"http://local/{path}"),
                             lambda: None)
            for tasks, path in [(a, "a1"), (a, "a2"), (a, "a3"), (b, "b1")]]
    await asyncio.sleep(0)
    assert list(sent) == ["http://local/a1"]
    sent["http://local/a1"].set_result("a1")
    await asyncio.wrap_future(jobs[0].result)
    await asyncio.wait_for(b_started.wait(), 1)
    assert list(sent) == ["http://local/a1", "http://local/b1"]
    for job in jobs[1:3]:
        job.cancel()
        assert job.result.cancelled()
    sent["http://local/b1"].set_result("b1")
    assert await asyncio.wrap_future(jobs[3].result) == "b1"
    assert not a.pending and not b.pending


async def test_queue_limits_and_origin_limit_leave_room_for_another_origin():
    sent = {}

    def start(job):
        request = job.request
        sent[request.url] = Future()
        return sent[request.url]

    scheduler = HttpScheduler(start, limit=3, navigation_slots=0, per_origin=1,
                              queue_limit=2, page_queue_limit=1)
    a, b = Tasks(), Tasks()

    def submit(tasks, url):
        return scheduler.submit(tasks, tasks, NetworkRequest(url), lambda: None)

    first = submit(a, "http://one/a")
    blocked = submit(a, "http://one/b")
    with pytest.raises(HostStateError, match="queue is full"):
        submit(a, "http://two/rejected")
    also_blocked = submit(b, "http://one/c")
    c = Tasks()
    with pytest.raises(HostStateError, match="queue is full"):
        submit(c, "http://two/d")
    also_blocked.cancel()
    other = submit(b, "http://two/c")
    await asyncio.sleep(0)
    assert set(sent) == {"http://one/a", "http://two/c"}
    blocked.cancel()
    for future in sent.values():
        future.set_result(None)
    await asyncio.gather(*(asyncio.wrap_future(job.result) for job in (first, other)))
    assert blocked.result.cancelled()
    assert not a.pending and not b.pending


async def test_cancelled_observer_is_not_sent_before_its_finally_runs():
    tasks, seen = Tasks(), []
    gate = Future()

    def start(job):
        seen.append(job.request.url)
        return gate

    scheduler = HttpScheduler(start, limit=1, navigation_slots=0)
    request = NetworkRequest("http://local/")
    scheduler.submit(tasks, tasks, request, lambda: None)
    queued = scheduler.submit(tasks, tasks, request, lambda: None)
    queued.result.cancel()
    gate.set_result(None)
    assert seen == [request.url]
    assert not tasks.pending


def test_blocking_reserve_is_bounded_and_cannot_consume_navigation_slots():
    sent = {}

    def start(job):
        return sent.setdefault(job.request.url.rsplit("/", 1)[-1], Future())

    scheduler = HttpScheduler(start, limit=4, navigation_slots=1, blocking_slots=1, per_page=2)
    a, b = Tasks(), Tasks()

    def submit(tasks, path, **lane):
        return scheduler.submit(tasks, tasks, NetworkRequest(f"http://local/{path}"),
                                lambda: None, **lane)

    submit(a, "ordinary-1")
    submit(a, "ordinary-2")
    submit(a, "sync-1", blocking=True)
    second = submit(b, "sync-2", blocking=True)
    assert list(sent) == ["ordinary-1", "ordinary-2", "sync-1"]
    submit(b, "navigation", navigation=True)
    assert list(sent)[-1] == "navigation"
    assert scheduler.active_count == 4
    sent["sync-1"].set_result(None)
    assert list(sent)[-1] == "sync-2"
    assert scheduler.active_count == 4
    for future in sent.values():
        if not future.done():
            future.set_result(None)
    assert second.result.result() is None
    assert not a.pending and not b.pending


def test_small_pool_shares_unreserved_slots_without_starving_ordinary_requests():
    sent = {}

    def start(job):
        future = Future()
        sent[job.request.url.rsplit("/", 1)[-1]] = future
        return future

    scheduler = HttpScheduler(start, limit=2, navigation_slots=0)
    tasks = Tasks()
    for path, lane in [
        ("navigation", {"navigation": True}), ("ordinary-1", {}),
        ("sync-1", {"blocking": True}), ("sync-2", {"blocking": True}),
        ("ordinary-2", {}),
    ]:
        scheduler.submit(tasks, tasks, NetworkRequest(f"http://local/{path}"), lambda: None, **lane)

    assert list(sent) == ["navigation", "ordinary-1"]
    sent["ordinary-1"].set_result(None)
    assert list(sent)[-1] == "sync-1"
    sent["sync-1"].set_result(None)
    assert list(sent)[-1] == "ordinary-2"
    sent["ordinary-2"].set_result(None)
    assert list(sent)[-1] == "sync-2"
    sent["sync-2"].set_result(None)
    sent["navigation"].set_result(None)
    assert not tasks.pending


def test_a_queued_deadline_settles_without_waiting_for_the_owner_or_a_slot():
    tasks, sent = Tasks(), []
    gate = Future()

    def start(job):
        sent.append(job.request.url)
        return gate

    scheduler = HttpScheduler(start, limit=1, navigation_slots=0)
    scheduler.submit(tasks, tasks, NetworkRequest("http://local/hold"), lambda: None)
    deadline = time.perf_counter() + .05

    def remaining():
        value = deadline - time.perf_counter()
        if value <= 0:
            raise TimeoutError
        return value

    queued = scheduler.submit(tasks, tasks, NetworkRequest("http://local/expired"), remaining)
    with pytest.raises(TimeoutError, match="expired in the queue"):
        queued.result.result(timeout=2)
    assert sent == ["http://local/hold"] and len(tasks.pending) == 1
    gate.set_result(None)
    scheduler.close()
    assert not tasks.pending
    assert not scheduler._watchdog.is_alive()
