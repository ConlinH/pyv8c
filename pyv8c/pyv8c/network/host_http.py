"""Host HTTP policy and terminal delivery from the Profile scheduler."""
import asyncio
from concurrent.futures import Future
from contextvars import copy_context
from dataclasses import replace

from ..foundation.futures import bridge_future
from ..foundation.host_errors import LoadFailure, RequestTimeout
from ..foundation.load_handle import LoadHandle
from .models import with_cookie_header
from .redirects import Deliver, RedirectFailure, next_hop


async def request(backend, tasks, group, request, remaining):
    """Submit one host request and publish only its terminal response to Python."""
    result, handle = Future(), LoadHandle()
    context = copy_context()
    hops = _redirect_hops(backend.http, backend.cookies, request)

    def fail(error):
        if handle.finish() and result.set_running_or_notify_cancel():
            result.set_exception(error)

    def advance(value):
        if handle.done or result.cancelled():
            return
        try:
            hop = hops.send(value)
        except StopIteration as terminal:
            if handle.finish() and result.set_running_or_notify_cancel():
                result.set_result(terminal.value)
            return
        except BaseException as error:
            fail(error)
            return
        try:
            job = backend.submit(tasks, group, hop, remaining, host=True, bind=handle.bind_transport)
        except BaseException as error:
            fail(error)
            return
        handle.observe_transport(job, job.result, completed)

    def completed(done):
        if done.cancelled():
            if handle.finish():
                result.cancel()
            return
        try:
            value = done.result()
        except BaseException as error:
            fail(error)
        else:
            context.copy().run(advance, value)

    def expire():
        if handle.cancel() and result.set_running_or_notify_cancel():
            result.set_exception(RequestTimeout("HTTP request exceeded its deadline"))

    advance(None)
    timer = None
    try:
        try:
            budget = remaining()
        except RequestTimeout:
            budget = 0
        if budget is not None:
            # The timer competes with transport settlement on the request handle.
            timer = asyncio.get_running_loop().call_later(budget, expire)
        return await bridge_future(result)
    finally:
        if timer is not None:
            timer.cancel()
        if handle.cancel():
            result.cancel()


def _redirect_hops(engine, jar, request):
    """Apply host redirect rules with fresh admission and cookies on each hop."""
    engine_follows = getattr(engine, "FOLLOWS_ITSELF", False)
    follow = request.redirect == "follow" and request.allow_redirects is not False
    current = request
    hops, crossed = 0, False
    while True:
        response = yield current if engine_follows else replace(current, redirect="manual")
        if not follow or engine_follows:
            return response
        try:
            decision = next_hop(status=response.status, method=current.method, url=current.url,
                                headers=current.headers, body=current.body, hops=hops,
                                location=response.headers.get("location"), mode=current.mode,
                                redirect="follow")
        except RedirectFailure as error:
            raise LoadFailure(f"{request.method} {request.url} failed: {error}") from error
        if isinstance(decision, Deliver):
            return replace(response, redirected=True, redirect_crossed=crossed) if hops else response
        crossed |= decision.cross_origin
        headers = {key: value for key, value in decision.headers.items()
                   if key.lower() != "cookie"
                   or not (current.generated_cookie or decision.cross_origin)}
        current = with_cookie_header(replace(
            current, url=decision.url, method=decision.method, body=decision.body,
            headers=headers, generated_cookie=False,
            cookie_override=() if decision.cross_origin else current.cookie_override,
        ), jar)
        hops += 1
