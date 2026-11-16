"""Redirects, offline: a page's own loader answers every hop.

The same table the Chrome comparison pins is held here without a browser, so the
follow rules stay covered on a machine with no reference installed.  The loader
records ``(method, path, body)`` for every request it answers, which is what makes
method rewriting and hop counts assertable rather than inferred.
"""
import json
import time

import pytest

pytest.importorskip("pyv8c.pyv8")

from pyv8c import Browser
from pyv8c.web.fetch.response import Response

ORIGIN = "http://site.test"
OTHER = "http://other.test"
PAGE = "<!doctype html><title>page</title><body>page</body>"

#: path -> (status, headers, body) for the first server, and the same for the other.
ROUTES = {
    "/a": (302, {"location": "/b"}, ""),
    "/b": (303, {"location": "/c"}, ""),
    "/c": (200, {}, "landed"),
    "/post": (302, {"location": "/after"}, ""),
    "/keep": (307, {"location": "/after"}, ""),
    "/after": (200, {}, "landed"),
    "/no-location": (302, {}, ""),
    "/bad-location": (302, {"location": "http://[bad"}, ""),
    "/cross": (302, {"location": OTHER + "/plain"}, ""),
    "/cross-ok": (302, {"location": OTHER + "/open"}, ""),
    "/nav/one": (302, {"location": "/nav/two"}, ""),
    "/nav/two": (302, {"location": "/nav/three"}, ""),
    "/nav/three": (200, {"content-type": "text/html"}, PAGE),
    "/nav/cross": (302, {"location": OTHER + "/nav-page"}, ""),
    "/form": (302, {"location": "/after"}, ""),
}
OTHER_ROUTES = {"/plain": (200, {}, "plain"), "/open": (200, {"access-control-allow-origin": "*"}, "open"),
                "/nav-page": (200, {"content-type": "text/html"}, PAGE), "/nav/back": (302, {"location": ORIGIN + "/after"}, "")}

WIRE = []


def _path_of(url):
    for prefix in (ORIGIN, OTHER):
        if url.startswith(prefix):
            return url[len(prefix):] or "/"
    return url


def _loader(url, request):
    path = _path_of(str(url))
    WIRE.append((str(request.get("method", "GET")).upper(), path, request.get("body")))
    table = ROUTES if str(url).startswith(ORIGIN) else OTHER_ROUTES
    if path in ("/start", "/after") and url.startswith(ORIGIN):
        spec = (200, {"content-type": "text/html"}, PAGE) if path == "/start" else (200, {}, "landed")
    elif path.startswith("/chain/"):
        _blank, _chain, total, index = path.split("/")
        if int(index) < int(total):
            spec = (302, {"location": "/chain/%s/%d" % (total, int(index) + 1)}, "")
        else:
            spec = (200, {}, "chain-end")
    else:
        spec = table.get(path)
    if spec is None:
        return Response("missing", {"status": 404, "url": str(url)})
    status, headers, body = spec
    return Response(body, {"status": status, "headers": headers, "url": str(url)})


@pytest.fixture
def page():
    browser = Browser(resource_loader=_loader, stub_logging="off")
    instance = browser.new_profile().new_page(ORIGIN + "/start")
    del WIRE[:]  # the document's own load is not part of what a test asks for
    try:
        yield instance
    finally:
        browser.close()


def _ask(page, url, options=None):
    """Run one fetch and answer what it settled on, or the error it raised."""
    page.evaluate(
        "window.__r = null;"
        "fetch(%s, %s).then((r) => r.text().then((text) => { window.__r = JSON.stringify("
        "{text: text, status: r.status, url: r.url, redirected: r.redirected, type: r.type, ok: r.ok}); }))"
        ".catch((e) => { window.__r = 'ERR ' + e.name + ': ' + e.message; });"
        % (json.dumps(url), json.dumps(options or {}))
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        page.run_until_idle(timeout=1)
        answer = page.evaluate("window.__r")
        if answer is not None:
            return answer if str(answer).startswith("ERR") else json.loads(answer)
    raise AssertionError("the fetch never settled")


def _hops():
    return [(method, path) for method, path, _body in WIRE]


def test_a_fetch_follows_every_hop_of_a_chain(page):
    answer = _ask(page, "/a")
    assert answer == {"text": "landed", "status": 200, "url": ORIGIN + "/c",
                          "redirected": True, "type": "basic", "ok": True}
    assert _hops() == [("GET", "/a"), ("GET", "/b"), ("GET", "/c")]


@pytest.mark.parametrize("path,status", [("/post", 302), ("/keep", 307)])
def test_the_method_and_body_a_redirect_carries(page, path, status):
    answer = _ask(page, path, {"method": "POST", "body": "payload"})
    assert answer["url"] == ORIGIN + "/after"
    first, second = WIRE[0], WIRE[1]
    assert first == ("POST", path, "payload")
    if status == 302:
        assert second == ("GET", "/after", None)
    else:
        assert second == ("POST", "/after", "payload")


def test_a_missing_location_is_delivered_as_it_arrived(page):
    answer = _ask(page, "/no-location")
    assert (answer["status"], answer["redirected"], answer["url"]) == (302, False, ORIGIN + "/no-location")
    assert _hops() == [("GET", "/no-location")]


def test_a_location_that_cannot_be_resolved_fails_the_load(page):
    answer = _ask(page, "/bad-location")
    #: The page hears Chrome's wording; why the load failed stays on the host's side.
    assert answer == "ERR TypeError: Failed to fetch"
    assert _hops() == [("GET", "/bad-location")]


def test_a_manual_redirect_reads_as_opaque(page):
    answer = _ask(page, "/a", {"redirect": "manual"})
    assert (answer["status"], answer["type"], answer["redirected"], answer["ok"]) == \
        (0, "opaqueredirect", False, False)
    assert answer["url"] == ORIGIN + "/a" and answer["text"] == ""
    assert _hops() == [("GET", "/a")]


def test_a_redirect_mode_of_error_refuses_the_first_redirect(page):
    answer = _ask(page, "/a", {"redirect": "error"})
    assert answer == "ERR TypeError: Failed to fetch"
    assert _hops() == [("GET", "/a")]


def test_the_twenty_first_redirect_fails_and_the_twentieth_lands(page):
    assert _ask(page, "/chain/20/0")["text"] == "chain-end"
    assert len(_hops()) == 21
    del WIRE[:]
    answer = _ask(page, "/chain/21/0")
    assert answer == "ERR TypeError: Failed to fetch"
    assert len(_hops()) == 21


def test_an_intermediate_cross_origin_hop_must_pass_its_own_cors_check(page):
    answer = _ask(page, "/cross")
    assert answer == "ERR TypeError: Failed to fetch"
    assert _hops() == [("GET", "/cross"), ("GET", "/plain")]


def test_a_cross_origin_hop_that_allows_the_origin_lands(page):
    answer = _ask(page, "/cross-ok")
    assert answer["url"] == OTHER + "/open" and answer["redirected"] is True
    assert _hops() == [("GET", "/cross-ok"), ("GET", "/open")]


def test_an_xhr_reports_the_url_it_landed_on(page):
    page.evaluate(
        "window.__x = null; const x = new XMLHttpRequest(); x.open('GET', '/a');"
        " x.onload = () => { window.__x = JSON.stringify([x.status, x.responseURL, x.responseText]); };"
        " x.send();"
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and page.evaluate("window.__x") is None:
        page.run_until_idle(timeout=1)
    assert json.loads(page.evaluate("window.__x")) == [200, ORIGIN + "/c", "landed"]


def test_a_navigation_counts_its_redirects(page):
    page.goto(ORIGIN + "/nav/one")
    reported = page.evaluate("[location.href, performance.navigation.redirectCount,"
                             " performance.getEntriesByType('navigation')[0].redirectCount]")
    assert list(reported) == [ORIGIN + "/nav/three", 2, 2]
    assert page.evaluate("performance.timing.redirectStart >= performance.timing.navigationStart"
                         " && performance.timing.redirectEnd >= performance.timing.redirectStart")
    page.goto(ORIGIN + "/nav/cross")
    # A hop that crosses origins reports no count at all, as Chrome does.
    assert page.evaluate("performance.navigation.redirectCount") == 0
    assert page.evaluate("[performance.timing.redirectStart, performance.timing.redirectEnd]") == [0, 0]
    assert page.evaluate("location.href") == OTHER + "/nav-page"


def test_a_form_post_that_is_redirected_reaches_the_page_as_a_get(page):
    page.set_content("<form method=post action='/form'><input name=q value=1>"
                     "<button>go</button></form>", url=ORIGIN + "/start")
    page.evaluate("document.forms[0].submit()")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and page.evaluate("location.pathname") != "/after":
        page.run_until_idle(timeout=1)
    assert page.evaluate("location.href") == ORIGIN + "/after"
    assert [hop for hop in _hops() if hop[1] == "/form"] == [("POST", "/form")]
    assert ("GET", "/after") in _hops()
