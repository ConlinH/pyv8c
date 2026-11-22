"""Window execution ownership; foundation never depends on Page or V8."""
from pyv8c import pyv8
import threading
import sys
import time
from functools import partial

from ..foundation.agent_tasks import AgentTaskQueue
from ..foundation.rendering import RenderingScheduler
from ..foundation.error_policy import control_exceptions
from ..foundation.foreground import ForegroundSource
from ..foundation.heap_memory import HeapMemoryCache


class NavigationCancelled(RuntimeError):
    """A document commit lost its execution lane to page teardown."""


class Agent:
    __slots__ = ('origin', 'microtasks', 'checkpointing', 'tasks')

    def __init__(self, isolate, origin):
        self.origin = origin
        self.microtasks = pyv8.MicrotaskQueue(isolate)
        self.checkpointing = False
        self.tasks = AgentTaskQueue()


class WindowExecutionGroup:
    def __init__(self, *, executor, frame_rate=60.0):
        self.heap_memory = HeapMemoryCache()
        self.rendering = RenderingScheduler(frame_rate=frame_rate)
        self.executor = executor
        self.isolate = None
        try:
            self.executor.call(self.executor.attach_rendering, self.rendering)
            self.isolate = self.executor.call(pyv8.Isolate, thread_bound=True)
            from ..web.fetch.response import wasm_response_payload
            self.executor.call(self.isolate._set_wasm_response_handler, wasm_response_payload)
            self.foreground = ForegroundSource(self.isolate, self._run_foreground,
                                               self._has_foreground_context)
        except BaseException:
            try:
                if self.isolate is not None:
                    self.executor.call(self.isolate.close)
            finally:
                self.rendering.close()
                self.executor.close()
            raise
        self._foreground_attached = False
        self.agents = {}
        self.members = set()
        self._dirty = {}
        self._pending_rejections = {}
        self._checkpointing = False
        self.closed = False
        self._control_lock = threading.RLock()
        self._operation = None
        self._stopped_operations = set()
        self.stopping = False

    def _has_foreground_context(self):
        return any(not member.closed and member.context is not None
                   for member in tuple(self.members))

    def _foreground_endpoint(self):
        for member in tuple(self.members):
            context = member.context
            if (not member.closed and context is not None
                    and not getattr(context, 'closed', False)):
                return member, context
        return None, None

    def _run_foreground(self):
        endpoint, context = self._foreground_endpoint()
        if endpoint is None:
            return False
        try:
            return endpoint.invoke(context._run_foreground_task, _background=True)
        except RuntimeError:
            # A navigation can close the context between selection and entry.
            # Treat that as this turn's end; the executor will retry only if a
            # live endpoint remains runnable.
            if getattr(context, 'closed', False) or endpoint.closed:
                return False
            raise
        finally:
            if not self.closed and not self.stopping:
                for agent in self.agents.values():
                    self._dirty[agent] = None
                self.checkpoint()

    def is_idle(self, agent):
        """Whether *agent* has no ready command, task, frame, or foreground work."""
        if agent.checkpointing or agent in self._dirty:
            return False
        operation = self._operation
        if operation is not None and operation[0].agent is agent:
            return False
        lanes = {lane for lane in tuple(self.members)
                 if lane.agent is agent and not lane.closed}
        return (not self.executor.has_pending_for(lanes)
                and not self.foreground.has_runnable_tasks()
                and not any(lane.tasks.has_runnable_tasks() for lane in lanes)
                and not self.rendering.has_runnable_tasks(lanes))

    def agent(self, origin):
        if not self.executor.is_owner_thread():
            raise RuntimeError('Agent creation requires the group owner thread')
        key = tuple(origin)
        if key not in self.agents:
            self.agents[key] = Agent(self.isolate, key)
            self.executor.attach_task_queue(self.agents[key].tasks)
        return self.agents[key]

    def endpoint(self, tasks):
        endpoint = WindowExecutor(self, tasks)
        self.members.add(endpoint)
        return endpoint

    def request_stop(self, endpoint=None):
        """Restricted cross-thread control: only terminate a live native ticket."""
        with self._control_lock:
            if endpoint is None:
                self.stopping = True
            operation = self._operation
            if operation is None or (endpoint is not None and operation[0] is not endpoint):
                return False
            if operation[2] in self._stopped_operations:
                return True
            self._stopped_operations.add(operation[2])
            try:
                accepted = operation[1].terminate_operation(operation[2])
            except BaseException:
                self._stopped_operations.discard(operation[2])
                raise
            if not accepted:
                self._stopped_operations.discard(operation[2])
            return accepted

    def checkpoint(self):
        if self.closed or self._checkpointing or pyv8.current_context(self.isolate) is not None:
            return
        self._checkpointing = True
        try:
            while self._dirty:
                agent = next(iter(self._dirty))
                del self._dirty[agent]
                agent.checkpointing = True
                try:
                    # A live native operation already owns the deadline, including
                    # Inspector suspension. Do not replace it with a wall-clock
                    # estimate. A standalone checkpoint still needs a budget:
                    # otherwise foreground/error cleanup can re-enter jobs with
                    # an unlimited crossing after the task's ticket has ended.
                    if self._operation is not None:
                        budget = 0
                    elif self.executor.has_operation:
                        budget = self.executor.remaining_budget()
                        if budget is not None and budget <= 0:
                            raise pyv8.JavaScriptTerminated()
                    else:
                        budget = self.executor.default_timeout
                    agent.microtasks.checkpoint(timeout=budget or 0)
                    for lane, tracker in tuple(self._pending_rejections.items()):
                        if lane.agent is agent and lane.rejections is tracker and not lane.closed:
                            tracker.flush()
                finally:
                    agent.checkpointing = False
        finally:
            self._checkpointing = False

    def release(self, endpoint):
        if endpoint not in self.members:
            return
        endpoint.attach_rejection_tracker(None)
        self.executor.cancel_owner(endpoint)
        self.rendering.cancel_owner(endpoint)
        endpoint.tasks.close()
        if len(self.members) > 1:
            self.members.discard(endpoint)
            endpoint._closed = True
            return
        self.rendering.close()
        if self._foreground_attached:
            self.executor.detach_foreground(self.foreground)
            self._foreground_attached = False
        for agent in self.agents.values():
            self.executor.detach_task_queue(agent.tasks)
            agent.tasks.close()
            agent.microtasks.close()
        self.agents.clear()
        self._dirty.clear()
        self._pending_rejections.clear()
        self.isolate.close()
        self.members.discard(endpoint)
        endpoint._closed = True
        self.closed = True
        self.executor.close()

class WindowExecutor:
    """A page's execution lane; closing it does not close sibling lanes."""
    def __init__(self, group, tasks):
        self.group, self.tasks = group, tasks
        self.agent = None
        self.context = None
        self.rejections = None
        self._closed = False
        tasks.set_boundary(self.after_callback)

    def after_callback(self):
        if isinstance(sys.exception(), control_exceptions()):
            return
        operation = self.group._operation
        stopped = self.group.stopping or (operation is not None and operation[2] in self.group._stopped_operations)
        if not stopped and not self.closed and self.agent is not None:
            self.group._dirty[self.agent] = None
            self.group.checkpoint()

    @property
    def closed(self):
        return self._closed or self.group.executor.closed

    def is_owner_thread(self):
        return self.group.executor.is_owner_thread()

    def remaining_budget(self):
        remaining = self.group.executor.remaining_budget()
        operation = self.group._operation
        if operation is not None and operation[3] is not None:
            native = max(0.0, operation[3] - time.perf_counter())
            remaining = native if remaining is None else min(remaining, native)
        return remaining

    def check_operation(self):
        """Keep owner-side host waits inside the active native operation."""
        with self.group._control_lock:
            operation = self.group._operation
            remaining = self.remaining_budget()
            stopped = (self.closed or self.group.stopping
                       or (operation is not None and operation[2] in self.group._stopped_operations))
            if stopped or (remaining is not None and remaining <= 0):
                if operation is not None:
                    operation[1].terminate_operation(operation[2])
                raise pyv8.JavaScriptTerminated()

    def invoke(self, callback, *args, _background=False, _owns_checkpoint=False, **kwargs):
        if self.closed:
            raise RuntimeError('window executor is closed')
        if self.agent is not None:
            self.group._dirty[self.agent] = None
        context = self.context
        armed = context is not None and pyv8.current_context(self.group.isolate) is None
        operation = previous_operation = None
        if armed:
            driver = self.group.executor
            budget = driver.remaining_budget() if driver.has_operation else driver.default_timeout
            with self.group._control_lock:
                if (self.group.stopping and not driver.has_operation
                        and self.group._operation is None):
                    return None
                if budget is not None and budget <= 0:
                    raise pyv8.JavaScriptTerminated()
                deadline = None if budget is None else time.perf_counter() + budget
                ticket = context.begin_operation(budget or 0)
                operation = (self, context, ticket, deadline)
                previous_operation = self.group._operation
                self.group._operation = operation
        try:
            try:
                return callback(*args, **kwargs)
            finally:
                if not _owns_checkpoint:
                    self.after_callback()
        except pyv8.JavaScriptTerminated as error:
            if not _background or operation is None:
                raise
            with self.group._control_lock:
                stopped = operation[2] in self.group._stopped_operations
            if not stopped:
                error.add_note(f'Window operation={operation[2] if operation else None}, realm={id(context)}')
                self.tasks.report_error(error, callback)
        finally:
            if operation is not None:
                with self.group._control_lock:
                    context.end_operation(operation[2])
                    self.group._stopped_operations.discard(operation[2])
                    self.group._operation = previous_operation

    def call(self, callback, *args, _owns_checkpoint=False, **kwargs):
        # Script operations own their checkpoint before taking a result snapshot.
        # Other host operations keep the same invoke boundary, including failures.
        lane = self.tasks if self.agent is not None else None
        return self.group.executor.call(self.invoke, callback, *args, _task_lane=lane,
                                        _owns_checkpoint=_owns_checkpoint, **kwargs)

    def close_page(self, callback):
        """Owner teardown remains reachable after document sources stop accepting work."""
        if self.group.executor.failed and not self.group.stopping:
            self.group.request_stop()
        return self.group.executor.call(self.invoke, callback, timeout=self.group.executor.default_timeout)

    def cancel_pending(self):
        self.group.executor.cancel_owner(self)

    def post(self, callback, *args, **kwargs):
        lane = self.tasks if self.agent is not None else None
        return self.group.executor.post(self.invoke, self._run_post, callback, args, kwargs, _task_lane=lane, _background=True)

    def _run_post(self, callback, args, kwargs):
        # Fire-and-forget commands have no Future to receive an exception.
        # Route it to this window while its native operation is still active.
        try:
            return callback(*args, **kwargs)
        except control_exceptions():
            raise
        except BaseException as error:
            self.tasks.report_error(error)

    def bind_agent(self, agent, context):
        if self.tasks.closed:
            raise NavigationCancelled('navigation cancelled while closing the page')
        try:
            self.tasks.bind_scheduler(agent.tasks, partial(self.invoke, _background=True), context)
        except RuntimeError as error:
            # request_stop() may flip the host's closed state after the check.
            if self.tasks.closed and str(error) == 'cannot bind a closed task queue':
                raise NavigationCancelled('navigation cancelled while closing the page') from None
            raise
        self.agent = agent
        if not self.group._foreground_attached:
            self.group.executor.attach_foreground(self.group.foreground)
            self.group._foreground_attached = True
        else:
            self.group.executor.wakeup()

    def attach_rejection_tracker(self, tracker):
        if self.rejections is not None:
            self.rejections.set_pending_listener(None)
        self.rejections = tracker
        if tracker is not None:
            tracker.set_pending_listener(self._rejection_pending_changed)

    def _rejection_pending_changed(self, tracker, pending):
        if pending and tracker is self.rejections:
            self.group._pending_rejections[self] = tracker
        elif self.group._pending_rejections.get(self) is tracker:
            del self.group._pending_rejections[self]

    def attach_deadline_source(self, context):
        self.context = context

    def detach_deadline_source(self, context):
        if self.context is context:
            self.context = None
            self.agent = None

    def close(self):
        self.group.release(self)
