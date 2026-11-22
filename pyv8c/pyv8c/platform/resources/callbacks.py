"""Host callback execution; Fetcher queues the raw result on its owner."""
from contextvars import copy_context
import threading

from ...foundation.host_errors import RequestTimeout


class CallbackRunner:
    def __init__(self, page):
        self.page = page
        self.browser = page.profile.browser

    def dispatch(self, call, tail, *, force_pool=False, kind="host-callback"):
        """Run the host callback and raw-result tail inline or on the pool."""
        browser = self.browser
        def gather():
            try:
                return call(), None
            except BaseException as exc:
                return None, exc
        if not browser.threaded_loaders and not force_pool:
            value, error = gather()
            tail(value, error)
            return None
        def pool_side():
            value, error = gather()
            tail(value, error)
        try:
            return self.page._background.submit(browser.loader_pool, copy_context().run, pool_side, kind=kind)
        except BaseException as exc:
            tail(None, exc)
            return None

    def call_sync(self, call, remaining, *, cache=False):
        """Run *call* and wait on this thread for its result.

        The wait is a plain event rather than the realm's queue: this path is reached
        from inside a turn (a parser-blocking script, an iframe, a worker source),
        where nothing else can run the queue for it.
        """
        if not self.browser.threaded_loaders and not cache:
            remaining()
            return call()
        executor = self.page.executor
        remaining()
        arrived = threading.Event()
        future = self.page._background.submit(self.browser.loader_pool, copy_context().run, call,
                                              kind="resource-cache" if cache else "host-callback")
        future.add_done_callback(lambda done: arrived.set())
        try:
            while not future.done():
                executor.check_operation()
                try:
                    request_budget = remaining()
                except RequestTimeout as exc:
                    if future.done():
                        break
                    if cache:
                        raise
                    raise RequestTimeout("the page's loader did not answer before the request deadline") from exc
                budget = executor.remaining_budget()
                waits = [0.01]
                if request_budget is not None:
                    waits.append(request_budget)
                if budget is not None:
                    waits.append(budget)
                arrived.wait(max(0.0, min(waits)))
            executor.check_operation()
            return future.result()
        finally:
            future.cancel()


