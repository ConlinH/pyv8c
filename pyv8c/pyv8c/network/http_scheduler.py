"""Bounded Profile HTTP admission shared by all realms and host callers."""

from collections import Counter, OrderedDict, deque
from concurrent.futures import CancelledError, Future
from contextvars import copy_context
from inspect import iscoroutine
from threading import Condition, RLock, Thread, current_thread
from time import perf_counter
from urllib.parse import urlsplit

from ..foundation.host_errors import HostStateError, RequestTimeout
from .models import RequestHandle

_LANES = ("navigation", "blocking", "ordinary")


class HttpJob:
    def __init__(self, scheduler, tasks, group, request, remaining, lane, host):
        self.scheduler = scheduler
        self.tasks = tasks
        self.group = group
        self.request = request
        self.remaining = remaining
        self.lane = lane
        self.host = host
        self.context = copy_context()
        url = urlsplit(request.url)
        self.origin = (url.scheme, url.hostname, url.port or {"http": 80, "https": 443}.get(url.scheme))
        self.handle = RequestHandle()
        self.result = Future()
        self.active = False
        self.finished = False
        self.cancel_requested = False

    def cancel(self):
        self.scheduler.cancel(self)


class HttpScheduler:
    """Admission never waits for a Profile or Worker owner to pump its queue."""

    def __init__(self, start, *, limit=10, navigation_slots=2, blocking_slots=0, per_page=4, per_origin=6,
                 queue_limit=128, page_queue_limit=32):
        self._start = start
        self._limit, self._reserved = limit, navigation_slots
        self._blocking_slots = blocking_slots
        self._per_page, self._per_origin = per_page, per_origin
        self._queue_limit, self._page_queue_limit = queue_limit, page_queue_limit
        self._queues = {lane: OrderedDict() for lane in _LANES}
        self._lock = RLock()
        self._deadline_changed = Condition(self._lock)
        self._deadlines = {}
        self._watchdog = None
        self._closed = False
        self._queued = Counter()
        self._active = Counter()
        self._by_page, self._by_origin = Counter(), Counter()
        self._host_by_page = Counter()
        self._pumping = False
        self._last_shared_lane = "ordinary"
        self._last_group = {}

    @property
    def active_count(self):
        with self._lock:
            return self._active.total()

    def submit(self, tasks, group, request, remaining, *, navigation=False, blocking=False,
               host=False, bind=None):
        submitted = perf_counter()
        budget = remaining()
        lane = "navigation" if navigation else "blocking" if blocking else "ordinary"
        job = HttpJob(self, tasks, group, request, remaining, lane, host)
        with self._lock:
            if self._closed:
                raise HostStateError("HTTP scheduler is closed")
            queue = self._queues[lane]
            if (self._queued[lane] >= (self._queue_limit if lane == "ordinary" else 16)
                    or len(queue.get(group, ())) >= self._page_queue_limit):
                raise HostStateError("HTTP request queue is full")
            tasks.begin_network(job.cancel)
            queue.setdefault(group, deque()).append(job)
            self._queued[lane] += 1
            if bind is not None:
                bind(job)
        self._pump()
        if budget is not None:
            with self._lock:
                if not job.active and not job.finished and not self._closed:
                    self._deadlines[job] = submitted + budget
                    if self._watchdog is None:
                        self._watchdog = Thread(target=self._watch_deadlines, name="pyv8c-http-deadlines",
                                                daemon=True)
                        self._watchdog.start()
                    self._deadline_changed.notify()
        return job

    def _watch_deadlines(self):
        while True:
            with self._deadline_changed:
                if self._closed:
                    return
                if not self._deadlines:
                    self._deadline_changed.wait()
                    continue
                job = min(self._deadlines, key=self._deadlines.__getitem__)
                budget = self._deadlines[job] - perf_counter()
                if budget > 0:
                    self._deadline_changed.wait(budget)
                    continue
                self._remove(job)
                job.finished = True
            job.tasks.complete_network(job.cancel)
            if job.result.set_running_or_notify_cancel():
                job.result.set_exception(RequestTimeout("HTTP request expired in the queue"))

    def close(self):
        with self._lock:
            self._closed = True
            queued = [job for groups in self._queues.values() for queue in groups.values() for job in queue]
            watchdog = self._watchdog
            self._deadline_changed.notify()
        for job in queued:
            job.cancel()
        if watchdog is not None and watchdog is not current_thread():
            watchdog.join()

    def _remove(self, job):
        groups = self._queues[job.lane]
        queue = groups[job.group]
        queue.remove(job)
        self._queued[job.lane] -= 1
        if self._deadlines.pop(job, None) is not None:
            self._deadline_changed.notify()
        if not queue:
            del groups[job.group]

    def cancel(self, job):
        with self._lock:
            if job.finished or job.cancel_requested:
                return
            job.cancel_requested = True
            active = job.active
            if not active:
                self._remove(job)
                job.finished = True
        if active:
            job.context.copy().run(job.handle.cancel)
        else:
            job.tasks.complete_network(job.cancel)
            job.result.cancel()

    def _lane_full(self, lane):
        if lane == "navigation":
            return self._active[lane] >= max(1, self._reserved)
        ordinary = self._limit - self._reserved - self._blocking_slots
        if self._blocking_slots:
            return self._active[lane] >= (ordinary if lane == "ordinary" else self._blocking_slots)
        return (self._active["ordinary"] + self._active["blocking"] >= ordinary
                or (lane == "blocking" and self._active[lane] >= 1))

    def _next(self):
        if self._closed or self._active.total() >= self._limit:
            return None
        order = _LANES
        if self._limit == 1:
            # A single physical connection has to be shared across lanes.
            next_lane = _LANES.index(self._last_shared_lane) + 1
            order = _LANES[next_lane:] + _LANES[:next_lane]
        elif not self._blocking_slots and self._last_shared_lane == "blocking":
            # Alternate contenders for the ordinary budget, after navigation.
            order = ("navigation", "ordinary", "blocking")
        for lane in order:
            if self._lane_full(lane):
                continue
            groups = self._queues[lane]
            last = self._last_group.get(lane)
            if last in groups:
                groups.move_to_end(last)
            for group, queue in tuple(groups.items()):
                if self._by_page[lane, group] >= (self._per_page if lane == "ordinary" else 1):
                    continue
                for job in queue:
                    if job.host and self._host_by_page[group] >= 2:
                        continue
                    if lane == "ordinary" and self._by_origin[job.origin] >= self._per_origin:
                        continue
                    self._remove(job)
                    if group in groups:
                        groups.move_to_end(group)
                    return job
        return None

    def _pump(self):
        with self._lock:
            if self._pumping:
                return
            self._pumping = True
        while True:
            with self._lock:
                job = self._next()
                if job is None:
                    self._pumping = False
                    return
                job.active = True
                if self._limit == 1 or job.lane != "navigation":
                    self._last_shared_lane = job.lane
                self._last_group[job.lane] = job.group
                self._active[job.lane] += 1
                self._by_page[job.lane, job.group] += 1
                if job.host:
                    self._host_by_page[job.group] += 1
                if job.lane == "ordinary":
                    self._by_origin[job.origin] += 1
            try:
                if job.cancel_requested or job.result.cancelled() or job.handle.cancelled:
                    raise CancelledError
                job.remaining()
                sent = job.context.run(self._start, job)
                if not isinstance(sent, Future):
                    if iscoroutine(sent):
                        sent.close()
                    raise TypeError("submit_request() must return a concurrent.futures.Future")
                sent.add_done_callback(lambda done, job=job: self._settle_future(job, done))
            except BaseException as exc:
                self._settle(job, error=exc)

    def _settle_future(self, job, sent):
        try:
            result = sent.result()
        except BaseException as exc:
            self._settle(job, error=exc)
        else:
            self._settle(job, result=result)

    def _settle(self, job, *, result=None, error=None):
        with self._lock:
            if job.finished:
                return
            job.finished = True
            job.handle.detach_abort()
            self._active[job.lane] -= 1
            counts = [(self._by_page, (job.lane, job.group))]
            if job.host:
                counts.append((self._host_by_page, job.group))
            if job.lane == "ordinary":
                counts.append((self._by_origin, job.origin))
            for ledger, key in counts:
                ledger[key] -= 1
                if not ledger[key]:
                    del ledger[key]
        job.tasks.complete_network(job.cancel)
        # An observer can stop waiting without ending transport ownership.
        if isinstance(error, CancelledError):
            job.result.cancel()
        elif job.result.set_running_or_notify_cancel():
            if isinstance(error, StopIteration):
                original = error
                error = RuntimeError("network operation raised StopIteration")
                error.__cause__ = original
            if error is None:
                job.result.set_result(result)
            else:
                job.result.set_exception(error)
        self._pump()
