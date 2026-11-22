"""Browser host adapters for the shared pyv8 DevTools service."""

from pyv8c.pyv8.devtools import DevToolsHandle, DevToolsOptions, create_service
from pyv8c.pyv8.devtools.scheduler import Host

__all__ = ["DevToolsHandle", "DevToolsOptions"]


class _InspectorExecutor:
    """Mailbox-only control adapter; independent of document task acceptance."""
    def __init__(self, window):
        self._window = window

    @property
    def closed(self):
        return self._window.closed

    def is_owner_thread(self):
        return self._window.is_owner_thread()

    def post(self, callback, *args):
        return self._window.group.executor.post(self._window.invoke, callback, *args)


class _WorkerInspectorExecutor:
    """Only the Inspector mailbox may bypass normal Worker task sources."""
    def __init__(self, executor):
        self._executor = executor

    @property
    def closed(self):
        return self._executor.closed

    def is_owner_thread(self):
        return self._executor.is_owner_thread()

    def post(self, callback, *args):
        return self._executor.post(callback, *args, _task_lane=None)


class BrowserDebugging:
    def __init__(self, profile, page, options):
        self.profile = profile
        self.targets = {}
        host = self.page_host(page)
        self.handle = create_service(host, options, on_close=self._did_close)
        self.router = self.handle._router
        self.targets[page] = self.handle._root
        page._devtools_target = self.handle._root
        self._register_descendants(page)

    @staticmethod
    def page_host(page, kind="page"):
        # Target discovery runs on the transport thread, including while JS is
        # paused. Read only the stored Python URL; never call a public owner API
        # or enqueue a request that would depend on that paused owner.
        return Host(page.v8_context, _InspectorExecutor(page.realm.executor),
                    lambda: page.window.location.href, page._retire, kind)

    def _register_descendants(self, page):
        for child in tuple(page._owned.frames):
            self.register_page(child, kind="iframe", parent=page, started=True)
            self._register_descendants(child)
        for worker in tuple(page._owned.workers):
            runtime = worker._runtime
            if runtime is not None:
                runtime.executor.post(self.register_worker, runtime, self.targets[page], started=True)

    def register_page(self, page, kind="page", parent=None, *, started=False):
        if page in self.targets:
            return self.targets[page]
        parent_target = self.targets.get(parent)
        if parent_target is None:
            return None
        target = self.router.register(self.page_host(page, kind), parent_target, started=started)
        self.targets[page] = target
        page._devtools_target = target
        return target

    def register_worker(self, runtime, parent, *, started=False):
        if runtime in self.targets:
            return self.targets[runtime]
        if parent is None or parent.closed or runtime._context is None:
            return None
        target = self.router.register(Host(
            runtime._context, _WorkerInspectorExecutor(runtime.executor), lambda: runtime.script_url,
            runtime.stop, "worker",
        ), parent, started=started)
        self.targets[runtime] = target
        runtime.debug_target = target
        runtime._devtools = self
        return target

    def unregister(self, realm):
        target = self.targets.pop(realm, None)
        if target is None:
            return
        self.router.unregister(target)
        if target.host.executor.is_owner_thread():
            target.mailbox.drain(target)

    def target_info_changed(self, page):
        target = self.targets.get(page)
        if target is not None and not target.closed:
            self.router.event("Target.targetInfoChanged", {"targetInfo": target.descriptor()})

    def _did_close(self):
        if self.profile._devtools is self:
            self.profile._devtools = None
        self.targets.clear()

    def close(self):
        self.handle.close()


def start_devtools(profile, page, *, options=None):
    adapter = BrowserDebugging(profile, page, options)
    profile._devtools = adapter
    return adapter.handle
