"""Raw resource results cross one networking boundary before touching the realm."""

import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

pytest.importorskip("pyv8c.pyv8")
from pyv8c.async_api import Browser
from pyv8c.foundation.tasks import TaskSource
from pyv8c.network.models import NetworkResponse
from pyv8c.platform.resources import fetcher as window_resources
from _http_fixtures import resolved


@pytest.mark.parametrize("threaded", [False, True])
@pytest.mark.parametrize("kind", ["local", "loader", "error", "interceptor", "intercept-local", "redirect"])
async def test_completion_uses_one_networking_task_per_external_result(monkeypatch, threaded, kind):
    host_threads = []
    def loader(url, request):
        host_threads.append(threading.get_ident())
        if kind == "error":
            raise ValueError("fixture failure")
        if kind == "redirect" and url.endswith("/start"):
            return {"status": 302, "headers": {"location": "/end"}, "body": ""}
        return "ok"

    options = dict(real_network=False, stub_logging="off", threaded_loaders=threaded,
                   resource_loader=loader)
    if kind == "interceptor":
        options["resource_interceptor"] = lambda url, request: "ok"
    elif kind == "intercept-local":
        options["resource_interceptor"] = lambda url, request: None
    async with Browser(**options) as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://fixture.test/", wait_until="commit"))
        posted, processing, answers = [], [], []
        arrived = threading.Event()
        original_post = page.tasks.post_task
        original_convert = window_resources.web_response

        def post(callback, source=TaskSource.DOM, **kwargs):
            if kwargs.get("label") == "resource-completion":
                posted.append(source)
            return original_post(callback, source, **kwargs)

        def convert(*args, **kwargs):
            processing.append(page.tasks._scheduler._current)
            assert page.executor.is_owner_thread()
            return original_convert(*args, **kwargs)

        monkeypatch.setattr(page.tasks, "post_task", post)
        monkeypatch.setattr(window_resources, "web_response", convert)

        def complete(response, error):
            assert page.executor.is_owner_thread()
            record = page.tasks._scheduler._current
            assert record.source is TaskSource.NETWORKING
            if processing:
                assert record is processing[-1], "conversion and delivery share a task"
            answers.append((response, error))
            arrived.set()

        def issue():
            url = "data:text/plain,ok" if kind in ("local", "intercept-local") else "http://fixture.test/start"
            handle = page.resource_fetcher.load_resource_async(url, {}, complete)
            assert not answers
            return handle

        handle = page.executor.call(issue)
        assert (await asyncio.to_thread(arrived.wait, 3))
        assert len(answers) == 1
        assert posted == [TaskSource.NETWORKING] * (2 if kind == "redirect" else 1)
        response, error = answers[0]
        if kind == "error":
            assert isinstance(error, ValueError)
        else:
            assert error is None
            assert response.body_text() == "ok"
        assert handle.done
        assert page.resource_ownership.active_count == 0
        if host_threads:
            owner_id = page.executor.call(threading.get_ident)
            assert all((ident != owner_id) == threaded for ident in host_threads)


async def test_duplicate_interceptor_result_does_not_start_two_transports(monkeypatch):
    async with Browser(real_network=False, stub_logging="off",
                         resource_interceptor=lambda *args: None,
                         resource_loader=lambda *args: "ok") as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://fixture.test/", wait_until="commit"))
        calls, answers = [], []
        arrived = threading.Event()

        def dispatch(call, tail, **kwargs):
            calls.append(call)
            value = call()
            tail(value, None)
            if len(calls) == 1:
                tail(value, None)

        monkeypatch.setattr(page.resource_fetcher, "callbacks", SimpleNamespace(dispatch=dispatch))
        page.executor.call(lambda: page.resource_fetcher.load_resource_async(
            "http://fixture.test/result", {},
            lambda *args: (answers.append(args), arrived.set())))
        assert (await asyncio.to_thread(arrived.wait, 3))
        assert len(calls) == 2
        assert len(answers) == 1
        assert answers[0][1] is None
        assert page.resource_ownership.active_count == 0


@pytest.mark.parametrize("action", ["deliver", "cancel", "stale", "reject"])
@pytest.mark.parametrize("worker_reply", [False, True])
async def test_raw_http_reply_is_guarded_before_conversion(monkeypatch, action, worker_reply):
    async with Browser(real_network=False, stub_logging="off") as browser:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="http://fixture.test/", wait_until="commit"))
        replies, answers, conversions = [], [], []
        def submit_request(request, handle, remaining):
            result = Future()
            replies.append(result)
            return result

        from pyv8c.network.backend import NetworkBackend
        engine = NetworkBackend(http=SimpleNamespace(submit_request=submit_request, close=lambda: None))
        fetcher = page.resource_fetcher
        monkeypatch.setattr(page.profile, "network_backend", engine)
        original = window_resources.web_response
        def convert(*args, **kwargs):
            conversions.append(threading.get_ident())
            return original(*args, **kwargs)
        monkeypatch.setattr(window_resources, "web_response", convert)
        arrived = threading.Event()

        def issue_and_reply():
            handle = fetcher.load_resource_async(
                "http://fixture.test/result", {},
                lambda *args: (answers.append(args), arrived.set()))
            if action == "reject":
                monkeypatch.setattr(page.tasks, "post_task", lambda *args, **kwargs: None)
            # A custom engine can report synchronously, before the initiating owner turn returns.
            raw = NetworkResponse(url="http://fixture.test/result", body="ok")
            def reply():
                replies[0].set_result(raw)
            if worker_reply:
                worker = threading.Thread(target=reply)
                worker.start()
                worker.join(3)
                assert not worker.is_alive()
            else:
                reply()
            assert not conversions and not answers
            if action == "cancel":
                handle.cancel()
            elif action == "stale":
                page.navigation.generation += 1
            return handle

        handle = page.executor.call(issue_and_reply)
        if action == "deliver":
            assert (await asyncio.to_thread(arrived.wait, 3))
            assert len(answers) == len(conversions) == 1
            assert answers[0][1] is None
        else:
            # Control calls must still work when normal posting is rejected.
            assert (await page.run_until_idle(timeout=3))
            assert not answers and not conversions
            assert handle.cancelled
        assert page.resource_ownership.active_count == 0


@pytest.mark.parametrize("caller", ["window", "host"])
async def test_transport_redirect_advances_while_the_runtime_is_busy(caller):
    from contextvars import ContextVar

    identity = ContextVar("request_identity")
    replies = Future()
    hops = []

    class Engine:
        def submit_request(self, request, handle, remaining):
            hops.append((request.url, identity.get()))
            return replies if request.url.endswith("/start") else resolved(
                NetworkResponse(request.url, body="ok"))

        def close(self):
            pass

    async with Browser(http_engine=Engine(), ws_engine=None, stub_logging="off") as browser:
        page = browser.new_page()
        url = "http://fixture.test"
        await page.set_content("<p>local</p>", url + "/")
        token = identity.set("original request")
        try:
            request = page.net.get if caller == "host" else page.net.fetch
            observer = asyncio.create_task(request("/start", timeout=3))
            await asyncio.sleep(0)
        finally:
            identity.reset(token)
        assert hops == [(url + "/start", "original request")]

        def reply():
            replies.set_result(NetworkResponse(url + "/start", status=302,
                                              headers={"location": "/end"}))

        thread = threading.Thread(target=reply)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        assert hops == [(url + "/start", "original request"), (url + "/end", "original request")]
        assert not observer.done()
        assert (await observer).text == "ok"
