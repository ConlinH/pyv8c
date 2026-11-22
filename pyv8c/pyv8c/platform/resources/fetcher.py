"""Window adapter for Profile resource requests."""
from contextvars import copy_context
from dataclasses import dataclass
from functools import cached_property, partial
from threading import Event
from urllib.parse import urlsplit

from ...foundation.error_policy import control_exceptions
from ...foundation.host_errors import LoadFailure
from ...foundation.load_handle import LoadHandle
from ...foundation.realm import WorkerSource
from ...foundation.tasks import TaskSource
from ...foundation.urls import origin_tuple, serialize_origin
from ...network.models import NetworkRequest
from ...web.bom.utils import page_base_url
from .callbacks import CallbackRunner
from .request_headers import same_site
from .response import web_response
from .session import ResourceRequest, ResourceRequestSession


@dataclass(slots=True)
class WindowLoad:
    session: ResourceRequestSession
    request: ResourceRequest
    generation: int
    owner_realm: object = None


class ResourceFetchController:
    def __init__(self, page):
        self.page = page

    @cached_property
    def callbacks(self):
        return CallbackRunner(self.page)

    def load_worker_source_async(self, script_url, callback):
        def complete(response, error):
            source = None
            if error is None:
                try:
                    source = WorkerSource(self.worker_source(response, script_url),
                                          response.url or script_url)
                except control_exceptions():
                    raise
                except BaseException as exc:
                    error = exc
            callback(source, error)
        return self.load_resource_async(script_url, {
            'method': 'GET', 'mode': 'same-origin', 'destination': 'worker',
            'resource_type': 'script',
        }, complete, owner_realm=self.page.realm)

    @staticmethod
    def worker_source(response, script_url):
        if response.status and not 200 <= response.status < 300:
            raise LoadFailure(f'the worker script {script_url} answered {response.status}')
        return response.body_text()

    def prepare(self, url, options, *, owner_realm=None):
        page = self.page
        if page.closed or page.profile.closed:
            raise LoadFailure('Page is closed')
        origin = tuple(page.origin)
        initiator = str((options or {}).get('initiator_url') or page.url)
        if (options or {}).get('initiator_url') and urlsplit(initiator).scheme in ('http', 'https', 'blob'):
            origin = tuple(page.profile._blob_origins.get(initiator, origin_tuple(initiator)))
        top = page.top_page
        site, parent = serialize_origin(origin), page.parent_page
        while parent is not None:
            if not same_site(site, serialize_origin(parent.origin)):
                site = 'null'
                break
            parent = parent.parent_page
        session = ResourceRequestSession(
            profile=page.profile, environment=page.environment, origin=origin,
            base_url=page_base_url(page), loader=page.resource_loader,
            interceptor=page.resource_interceptor if callable(page.resource_interceptor) else None,
            backend=page.profile.network_backend, cache_enabled=page.cache_enabled, group=id(top),
            site_for_cookies=site, partition=serialize_origin(top.origin),
            top_level=page.parent_page is None, owner_streams=True,
            referrer_policy=page.navigation.referrer_policy,
        )
        request = session.prepare(url, options, initiator_url=initiator)
        return WindowLoad(session, request, page.navigation.generation, owner_realm)

    def active(self, load):
        # The transport reads these lifecycle markers without entering the Realm.
        page = self.page
        return (not page.closed and not page.profile.closed
                and (load.owner_realm is page.realm if load.owner_realm is not None
                     else load.generation == page.navigation.generation))

    def wait_network(self, load, request):
        job = load.session.submit(self.page.services.task_services, load.request, request, blocking=True)
        arrived = Event()
        job.result.add_done_callback(lambda done: arrived.set())
        try:
            while not arrived.is_set():
                self.page.executor.check_operation()
                try:
                    budget = load.request.remaining()
                except TimeoutError:
                    if arrived.is_set():
                        break
                    raise
                arrived.wait(.01 if budget is None else min(.01, budget))
            self.page.executor.check_operation()
            return job.result.result()
        finally:
            job.cancel()

    def load_resource(self, url, request=None):
        # Dismissal handlers cannot block retirement in a synchronous load.
        if self.page._retiring:
            raise LoadFailure('Page is closing; synchronous resource loads are unavailable')
        load = self.prepare(url, request)
        handle = LoadHandle()
        steps = load.session.steps(load.request, handle, lambda: not self.active(load))
        value = None
        try:
            while True:
                try:
                    step = steps.send(value)
                except StopIteration as terminal:
                    response = web_response(terminal.value, self.page.services)
                    handle.finish()
                    return response
                if isinstance(step, NetworkRequest):
                    value = self.wait_network(load, step)
                else:
                    value = self.callbacks.call_sync(step.call, load.request.remaining, cache=step.cache)
        finally:
            handle.cancel()
            steps.close()

    def load_resource_async(self, url, request, callback, *, owner_realm=None):
        start = self._start_async if owner_realm is None else partial(self._start_async, owner_realm=owner_realm)
        return self.page.resource_ownership.issue(start, url, request, callback,
                                                  document_bound=owner_realm is None)

    def _start_async(self, handle, url, request, callback, *, owner_realm=None):
        _AsyncLoad(self, handle, callback).start(url, request, owner_realm=owner_realm)


class _AsyncLoad:
    """Run host callbacks according to Window policy and deliver on its owner."""

    def __init__(self, fetcher, handle, callback):
        self.fetcher, self.callback = fetcher, callback
        self.handle = handle
        self.load = self.steps = None
        self.request_context = copy_context()

    def start(self, url, request, *, owner_realm=None):
        tasks = self.fetcher.page.tasks
        tasks.begin_loader()
        self.handle.add_done_callback(lambda _: tasks.complete_loader())
        try:
            self.load = self.fetcher.prepare(url, request, owner_realm=owner_realm)
            snapshot = self.load.request
            captured = None
            # Worker creation captures local source bytes before same-turn revocation.
            if snapshot.destination == 'worker' and snapshot.target.startswith(('blob:', 'data:')):
                try:
                    captured = self.load.session.local_response(snapshot)
                except control_exceptions():
                    raise
                except Exception as error:
                    captured = error
            load, fetcher, handle = self.load, self.fetcher, self.handle
            self.steps = load.session.steps(snapshot, handle, lambda: not fetcher.active(load),
                                           captured=captured)
            self.advance(None, None, in_task=False)
        except control_exceptions():
            self.handle.cancel()
            raise
        except BaseException as error:
            self.enqueue(self.settle, None, error)

    def active(self):
        return not self.handle.done and (self.load is None or self.fetcher.active(self.load))

    def enqueue(self, callback, *args):
        if self.handle.done:
            return
        try:
            posted = self.fetcher.page.tasks.post_task(
                partial(self.request_context.copy().run, callback, *args),
                TaskSource.NETWORKING, label='resource-completion')
        except BaseException:
            self.handle.cancel()
            raise
        if posted is None:
            self.handle.cancel()

    def settle(self, raw, error):
        self.steps = None
        if not self.active():
            self.handle.cancel()
            return
        try:
            response = web_response(raw, self.fetcher.page.services) if error is None else None
        except control_exceptions():
            self.handle.cancel()
            raise
        except BaseException as exc:
            response, error = None, exc
        if self.handle.finish():
            self.callback(response, error)

    def advance(self, value, error, *, in_task=True):
        if not self.active():
            self.handle.cancel()
            return
        try:
            if error is not None:
                raise error
            try:
                step = self.steps.send(value)
            except StopIteration as terminal:
                raw = terminal.value
            else:
                self.dispatch(step)
                return
        except control_exceptions():
            self.handle.cancel()
            raise
        except BaseException as exc:
            raw, error = None, exc
        if in_task:
            self.settle(raw, error)
        else:
            self.enqueue(self.settle, raw, error)

    def dispatch(self, step):
        if not self.active():
            self.handle.cancel()
            return
        network = isinstance(step, NetworkRequest)
        if not network and not step.cache and not self.fetcher.page.executor.is_owner_thread():
            self.enqueue(self.dispatch, step)
            return
        load = self.load
        stage = LoadHandle()
        self.handle.bind_transport(stage)

        def reply(value, error):
            if stage.finish():
                self.handle.unbind_transport(stage)
                if network or step.cache:
                    self.request_context.copy().run(self.advance, value, error, in_task=False)
                else:
                    self.enqueue(self.advance, value, error)

        if network:
            job = load.session.submit(self.fetcher.page.services.task_services, load.request, step,
                                      bind=stage.bind_transport)

            def completed(done):
                try:
                    value = done.result()
                except BaseException as error:
                    reply(None, error)
                else:
                    reply(value, None)
            stage.observe_transport(job, job.result, completed)
        else:
            force_pool = step.cache or load.request.mode == 'navigate' or load.request.destination == 'worker'
            def call():
                if stage.cancelled:
                    raise LoadFailure('resource request cancelled')
                return step.call()

            work = self.fetcher.callbacks.dispatch(call, reply, force_pool=force_pool,
                                                   kind='resource-cache' if step.cache else 'host-callback')
            stage.bind_transport(work)
