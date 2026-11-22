"""Agent-owned task sources, independent of Page, Realm and native bindings."""
from collections import deque
from dataclasses import dataclass
import itertools
import threading

from .tasks import TaskSource
from .logging import logger
from .error_policy import control_exceptions


class TaskTicket:
    """Cancellation and starting are one atomic transition."""
    __slots__ = ('_lock', '_state', '_record')

    def __init__(self, lock):
        self._lock = lock
        self._state = 'pending'
        self._record = None

    def _release(self):
        record, self._record = self._record, None
        if record is not None:
            record.callback = None
            record.context = None

    def _cancel_pending(self):
        with self._lock:
            if self._state != 'pending':
                return None
            self._state = 'cancelled'
            record, self._record = self._record, None
            if record is None:
                return ()
            retired = (record, record.callback, record.context)
            record.callback = record.context = None
        return retired

    def cancel(self):
        # Keep references alive until the cancellation lock has been released.
        retired = self._cancel_pending()
        return retired is not None

    def _start_locked(self):
        if self._state != 'pending':
            return False
        self._state = 'running'
        return True

    def _finish(self):
        # Once started, only the owner writes state or releases the record.
        # cancel() can no longer win, so completion needs no second lock.
        self._state = 'finished'
        self._release()

    @property
    def pending(self):
        with self._lock:
            return self._state == 'pending'


@dataclass(frozen=True, slots=True)
class TaskContext:
    realm: object = None
    document: object = None
    generation: int = 0


@dataclass(slots=True)
class TaskRecord:
    source: TaskSource
    sequence: int
    owner: object
    context: TaskContext
    ticket: TaskTicket
    callback: object
    label: str
    document_bound: bool = False
    timer_delivery: bool = False
    managed: bool = False

    def __post_init__(self):
        self.ticket._record = self


class AgentTaskQueue:
    """Fair sources shared by document lanes; timers remain lane-owned heaps.

    Only the owner thread selects and executes work. Posting, cancellation and
    closing are safe across threads. No lock is held while a callback executes.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._sources = {source: deque() for source in TaskSource}
        self._ready = deque()
        self._ready_set = set()
        self._lanes = {}
        self._timer_lanes = {}
        self._lane_sequence = itertools.count()
        self._sequence = itertools.count(1)
        self._closed = False
        self._wakeup = None
        self._current = None

    @property
    def closed(self):
        return self._closed

    def set_wakeup(self, wakeup):
        self._wakeup = wakeup

    def report_error(self, error):
        if self._current is not None:
            self._current.owner.report_error(error)
        else:
            logger.opt(exception=error).error('Agent task selection failed')

    def _signal(self):
        if self._wakeup is not None:
            self._wakeup()

    def attach(self, lane):
        with self._lock:
            if self._closed:
                raise RuntimeError('Agent task queue is closed')
            self._lanes.setdefault(lane, next(self._lane_sequence))
            self.refresh_timers(lane)

    def refresh_timers(self, lane):
        """Recheck live registrations, never a producer's stale snapshot.

        Callers must release the lane lock first: selection takes Agent then
        lane locks. Membership also rejects notifications after detach/close.
        """
        with self._lock:
            if not self._closed and lane in self._lanes and lane._has_timers():
                self._timer_lanes[lane] = self._lanes[lane]
            else:
                self._timer_lanes.pop(lane, None)

    def detach(self, lane):
        """Remove a lane's work without cancelling sibling document tasks."""
        with self._lock:
            self._lanes.pop(lane, None)
            self._timer_lanes.pop(lane, None)
            moved = []
            for source, entries in self._sources.items():
                remaining = deque()
                for record in entries:
                    (moved if record.owner is lane else remaining).append(record)
                self._sources[source] = remaining
            self._rebuild_ready()
        return moved

    def _rebuild_ready(self):
        self._ready = deque(source for source in self._ready if self._sources[source])
        self._ready_set = set(self._ready)

    def _enqueue(self, record):
        source = record.source
        self._sources[source].append(record)
        if source not in self._ready_set:
            self._ready.append(source)
            self._ready_set.add(source)

    def post(self, owner, callback, source, *, label=None, document_bound=False, timer_delivery=False, managed=False):
        with self._lock:
            if self._closed or owner.closed or owner not in self._lanes:
                return None
            ticket = TaskTicket(self._lock)
            record = TaskRecord(source, next(self._sequence), owner, owner._task_context,
                                ticket, callback, label or getattr(callback, '__qualname__', type(callback).__name__),
                                document_bound, timer_delivery, managed)
            self._enqueue(record)
        self._signal()
        return ticket

    def adopt(self, records):
        retired = []
        with self._lock:
            for record in sorted(records, key=lambda item: item.sequence):
                if not self._valid(record):
                    retired.extend((record, record.ticket._cancel_pending()))
                    continue
                record.sequence = next(self._sequence)
                self._enqueue(record)
        self._signal()
        return retired

    def invalidate_document(self, owner):
        retired = []
        with self._lock:
            for source, entries in self._sources.items():
                kept = deque()
                for record in entries:
                    if record.owner is owner and record.document_bound and not self._valid(record):
                        retired.extend((record, record.ticket._cancel_pending()))
                    else:
                        kept.append(record)
                self._sources[source] = kept
            self._rebuild_ready()
        return retired

    def _valid(self, record):
        # All callers hold this Agent's lock. Migrated records can still use
        # their former Agent's cancellation lock, which must remain acquired.
        ticket = record.ticket
        pending = ticket._state == 'pending' if ticket._lock is self._lock else ticket.pending
        return (pending and not record.owner.closed
                and (not record.document_bound or record.context is record.owner._task_context))

    def pending(self, owner=None):
        with self._lock:
            return any((owner is None or record.owner is owner) and self._valid(record)
                       for entries in self._sources.values() for record in entries)

    def has_runnable_tasks(self, owner=None):
        retired = None
        with self._lock:
            if self._closed:
                return False
            # Prune heads without rescanning all ready work on every turn.
            changed = runnable = False
            for source in tuple(self._ready):
                entries = self._sources[source]
                while entries and not self._valid(entries[0]):
                    record = entries.popleft()
                    if retired is None:
                        retired = []
                    retired.extend((record, record.ticket._cancel_pending()))
                if not entries:
                    changed = True
                elif not runnable:
                    # The non-parser head is already known to be valid. The
                    # selector will promote timers and recheck it before running.
                    if source is not TaskSource.PARSER and (owner is None or entries[0].owner is owner):
                        runnable = True
                    else:
                        runnable = any((owner is None or record.owner is owner) and self._valid(record)
                                       and (source is not TaskSource.PARSER or not record.owner._host.parser_blocked())
                                       for record in entries)
            if changed:
                self._rebuild_ready()
            if runnable:
                return True
            lanes = ((owner,) if owner in self._timer_lanes else ()) if owner is not None else self._timer_lanes
            return any(not lane.closed and lane._has_due_timer() for lane in lanes)

    def next_deadline(self):
        with self._lock:
            values = [value for lane in self._timer_lanes if not lane.closed
                      and (value := lane.next_deadline()) is not None]
            return min(values) if values else None

    def _promote_timers(self):
        # One queued timer delivery per lane. The heap keeps the real deadline
        # and cancellation identity; selection never executes a timer inline.
        due = sorted((lane._timer_deadline(), order, lane)
                     for lane, order in self._timer_lanes.items()
                     if not lane.closed and not lane._timer_queued and lane._has_due_timer())
        for _, _, lane in due:
            lane._timer_queued = True
            ticket = self.post(lane, lane._deliver_timer, TaskSource.TIMER,
                               label='timer delivery', timer_delivery=True)
            lane._timer_ticket = ticket
            if ticket is None:
                lane._timer_queued = False

    def run_turn(self, *, allow_parser=True):
        retired = None
        with self._lock:
            if self._closed:
                return False
            self._promote_timers()
            selected = None
            for _ in range(len(self._ready)):
                source = self._ready.popleft()
                self._ready_set.remove(source)
                entries = self._sources[source]
                blocked = []
                for _ in range(len(entries)):
                    record = entries.popleft()
                    if not self._valid(record):
                        if retired is None:
                            retired = []
                        retired.extend((record, record.ticket._cancel_pending()))
                        continue
                    if source is TaskSource.PARSER and (not allow_parser or record.owner._host.parser_blocked()):
                        blocked.append(record)
                        continue
                    if record.ticket._lock is self._lock:
                        started = record.ticket._start_locked()
                    else:
                        # A migrated record retains the cancellation lock held
                        # by its already-issued ticket.
                        with record.ticket._lock:
                            started = record.ticket._start_locked()
                    if started:
                        selected = record
                        break
                entries.extendleft(reversed(blocked))
                if entries:
                    self._ready.append(source)
                    self._ready_set.add(source)
                if selected is not None:
                    break
            if selected is None:
                return False
        if retired is not None:
            retired.clear()
        previous, self._current = self._current, selected
        try:
            # Timer delivery has its own callback/error/boundary machinery.
            if selected.timer_delivery or selected.managed:
                selected.callback()
            else:
                selected.owner.run_callback(selected.callback)
        except control_exceptions():
            raise
        except BaseException as error:
            selected.owner.report_error(error)
        finally:
            selected.ticket._finish()
            self._current = previous
        return True

    def close(self):
        retired = []
        with self._lock:
            self._closed = True
            for entries in self._sources.values():
                for record in entries:
                    retired.extend((record, record.ticket._cancel_pending()))
                entries.clear()
            retired.extend(self._lanes)
            self._lanes.clear()
            self._timer_lanes.clear()
            self._ready.clear()
            self._ready_set.clear()
        self._signal()
