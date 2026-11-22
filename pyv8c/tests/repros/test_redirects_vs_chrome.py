"""Redirect behaviour, compared with the local Chrome.

Both browsers are pointed at the same pair of local servers, which serve the same
redirect chains and record what arrived.  Every expectation here was observed on the
wire from the reference before it was pinned: 301/302 rewrite POST to GET, 303
rewrites everything but HEAD, 307/308 keep method and body, twenty redirects are
followed and the twenty-first fails, a missing ``Location`` is delivered as it
arrived while one that cannot be resolved fails, ``redirect: "manual"`` reads as
``opaqueredirect``, a cross-origin hop loses ``Authorization``, a ``Set-Cookie`` on a
redirect reaches the next hop, and a navigation counts its redirects -- zero once a
hop crossed origins.

A refused load is held to "both refused": the reason a load failed is the host's own, so
neither side shows it to the page.  One thing is still deliberately not compared: a
cross-origin response reads ``"cors"`` in Chrome but ``"basic"`` from the curl_cffi
bridge, a transport label this layer does not rewrite.
"""
from __future__ import annotations

import http.server
import json
import threading
from urllib.parse import urlparse

import pytest

pytest.importorskip("pyv8c.pyv8", reason="the comparison runs in the native environment")

from harness import ReferenceUnavailable, chrome_reference

PAGE = "<!doctype html><html><head><meta charset=utf-8><title>probe</title></head><body>probe</body></html>"

#: Cases that follow: name, request, and the status the first hop answers.
FOLLOWED = (
    ("follow301", {}, 301),
    ("follow302", {}, 302),
    ("follow303", {}, 303),
    ("follow307", {}, 307),
    ("follow308", {}, 308),
    ("not300", {}, 300),
    ("noLocation", {}, 302),
    ("nocors", {"mode": "no-cors"}, 302),
    ("chain20", {}, 302),
    ("cookieHop", {}, 302),
)

#: The method rewriting table, each case carrying a body a GET must not keep.
REWRITTEN = (
    ("post301", {"method": "POST", "body": "payload"}, 301),
    ("post302", {"method": "POST", "body": "payload"}, 302),
    ("post303", {"method": "POST", "body": "payload"}, 303),
    ("post307", {"method": "POST", "body": "payload"}, 307),
    ("post308", {"method": "POST", "body": "payload"}, 308),
    ("put302", {"method": "PUT", "body": "payload"}, 302),
    ("head303", {"method": "HEAD"}, 303),
)

#: ``manual`` reads as an opaque redirect, whatever the mode.
OPAQUE = (("manual", {"redirect": "manual"}, 302),
          ("manualNocors", {"redirect": "manual", "mode": "no-cors"}, 302))

#: Cases whose only agreement is that both refused the load.
REFUSED = (
    ("badLocation", {}, 302),
    ("errorMode", {"redirect": "error"}, 302),
    ("chain21", {}, 302),
    ("crossPlain", {}, 302),
    ("hopCross", {}, 302),
)

#: Cross-origin cases that land, whose response-type label differs by transport.
CROSSED = (("crossOk", {}, 302),
           ("crossAuth", {"headers": {"authorization": "Bearer t"}}, 302))

#: The chains the two limit cases walk, and the fields not compared across origins.
CHAIN = {"chain20": "/chain/20/0", "chain21": "/chain/21/0"}
UNCOMPARED = ("type",)

CASES = FOLLOWED + REWRITTEN + OPAQUE + REFUSED + CROSSED
STATUS = {name: status for name, _init, status in CASES}
REWRITING = {name for name, _init, _status in REWRITTEN}

RECORDS: list[dict] = []
LOCK = threading.Lock()
SAME: dict = {}
OTHER: dict = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self._handle()

    do_POST = do_PUT = do_HEAD = do_GET

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("latin-1") if length else ""
        path = urlparse(self.path).path
        with LOCK:
            RECORDS.append({
                "method": self.command, "path": path,
                "authorization": self.headers.get("authorization"),
                "cookie": self.headers.get("cookie"),
            })
        status, headers, payload = self._answer(path, body)
        encoded = payload.encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if not any(name.lower() == "content-type" for name in headers):
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _answer(self, path, body):
        other, same = OTHER["origin"], SAME["origin"]
        name = path[1:]
        if path in ("/page", "/nav-page"):
            return 200, {"Content-Type": "text/html; charset=utf-8"}, PAGE
        if path == "/nav/cross":
            return 302, {"Location": "%s/nav-page" % other}, ""
        if path == "/nav/same1":
            return 302, {"Location": "/nav/same2"}, ""
        if path == "/nav/same2":
            return 302, {"Location": "/end"}, ""
        if path.startswith("/chain/"):
            _blank, _chain, total, index = path.split("/")
            if int(index) < int(total):
                return 302, {"Location": "/chain/%s/%d" % (total, int(index) + 1)}, ""
            return 200, {}, "chain-end"
        if name == "noLocation":
            return 302, {}, ""
        if name == "badLocation":
            return 302, {"Location": "http://[bad"}, ""
        if name == "cookieHop":
            return 302, {"Location": "/cookie-end", "Set-Cookie": "hop=1; Path=/"}, ""
        if name == "cookie-end":
            return 200, {}, "cookie=%s" % (self.headers.get("cookie") or "-")
        if name in ("crossOk", "crossAuth"):
            return 302, {"Location": "%s/end-cors-%s" % (other, name)}, ""
        if name.startswith("end-cors-"):
            return 200, {"Access-Control-Allow-Origin": "*"}, "cross-ok"
        if name == "crossPlain":
            return 302, {"Location": "%s/plain-%s" % (other, name)}, ""
        if name.startswith("plain-"):
            return 200, {}, "plain"
        if name == "hopCross":
            return 302, {"Location": "%s/mid-%s" % (other, name)}, ""
        if name.startswith("mid-"):
            return 302, {"Location": "%s/end-%s" % (same, name[4:])}, ""
        if name.startswith("echo-"):
            return 200, {}, "%s:%s" % (self.command, body or "-")
        if name.startswith("end-"):
            return 200, {}, "done"
        if name in STATUS:
            target = "/echo-%s" % name if name in REWRITING else "/end-%s" % name
            return STATUS[name], {"Location": target}, ""
        return 404, {}, "missing"


PROBE = """
const cases = %s;
window.__obs = {}; window.__done = false;
let pending = cases.length;
for (const [name, url, init] of cases) {
  const settled = (value) => { window.__obs[name] = value; pending -= 1;
                               if (pending === 0) window.__done = true; };
  fetch(url, init).then((r) => r.text().then((text) => settled(
    {ok: true, status: r.status, redirected: r.redirected, url: r.url, type: r.type, text: text})
  )).catch((e) => settled({ok: false, error: e.name}));
}
'started'
"""

NAVIGATION = """
[JSON.stringify(location.href), performance.navigation.redirectCount,
 performance.getEntriesByType('navigation')[0].redirectCount]
"""


def _values(value):
    """What an ``evaluate`` answered: Chrome deserializes it, pyv8c answers JSON text."""
    return value if isinstance(value, (list, dict)) else json.loads(value)


def _asks():
    listed = []
    for name, init, _status in CASES:
        listed.append([name, CHAIN.get(name, "/" + name), init])
    return listed


def _requests():
    return [(record["method"], record["path"], record["authorization"], record["cookie"])
            for record in RECORDS]


def _for_case(records, name):
    """The requests one case made, in order: its own path, then what it led to."""
    if name in CHAIN:
        return [record for record in records if record[1].startswith("/chain/%s/" % name[5:])]
    return [record for record in records
            if record[1] == "/" + name or record[1].endswith("-" + name)]


@pytest.fixture(scope="module")
async def reference():
    try:
        async with chrome_reference() as session:
            yield session
    except ReferenceUnavailable as unavailable:
        pytest.skip(str(unavailable))


@pytest.fixture(scope="module")
async def compared(reference):
    """Both browsers against the same two servers: what each observed and sent."""
    servers = []
    for _ in range(2):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    SAME["origin"] = "http://127.0.0.1:%d" % servers[0].server_port
    OTHER["origin"] = "http://127.0.0.1:%d" % servers[1].server_port
    script = PROBE % json.dumps(_asks())
    try:
        with LOCK:
            RECORDS.clear()
        chrome_page = (await reference.browser.new_page())
        try:
            (await chrome_page.goto(SAME["origin"] + "/page", wait_until="load"))
            (await chrome_page.evaluate(script))
            (await chrome_page.wait_for_timeout(2000))
            chrome = _values((await chrome_page.evaluate("JSON.stringify(window.__obs)")))
            chrome_wire = _requests()
            chrome_navigation = (await _navigation(reference, SAME["origin"]))
        finally:
            (await chrome_page.close())

        from pyv8c.async_api import Browser

        with LOCK:
            RECORDS.clear()
        browser = (await Browser(real_network=True, stub_logging="off").__aenter__())
        try:
            page = browser.new_profile().new_page()
            (await page.goto(SAME["origin"] + "/page", wait_until="load"))
            page.evaluate(script)
            (await page.wait_for("window.__done"))
            mine = _values(page.evaluate("JSON.stringify(window.__obs)"))
            mine_wire = _requests()
            mine_navigation = (await _navigation(browser, SAME["origin"]))
        finally:
            (await browser.close())
        yield {"chrome": chrome, "pyv8c": mine, "chrome_wire": chrome_wire,
               "pyv8c_wire": mine_wire, "chrome_navigation": chrome_navigation,
               "pyv8c_navigation": mine_navigation}
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


async def _navigation(driver, origin):
    """What each side reports for a same-origin and a cross-origin navigation."""
    report = {}
    if hasattr(driver, "browser"):  # the Chrome reference
        page = await driver.browser.new_page()
        try:
            for label, path in (("same", "/nav/same1"), ("cross", "/nav/cross")):
                (await page.goto(origin + path, wait_until="load"))
                report[label] = _values(await page.evaluate(NAVIGATION))
        finally:
            await page.close()
        return report
    page = driver.new_profile().new_page()
    try:
        for label, path in (("same", "/nav/same1"), ("cross", "/nav/cross")):
            (await page.goto(origin + path, wait_until="load"))
            report[label] = _values(page.evaluate(NAVIGATION))
    finally:
        await driver.close()
    return report


async def test_a_followed_redirect_lands_where_chrome_lands(compared):
    for name, _init, _status in FOLLOWED:
        assert compared["pyv8c"][name] == compared["chrome"][name], name


async def test_a_method_is_rewritten_exactly_where_chrome_rewrites_it(compared):
    for name, _init, _status in REWRITTEN:
        assert compared["pyv8c"][name] == compared["chrome"][name], name


async def test_a_twenty_hop_chain_lands_where_chrome_lands(compared):
    theirs, ours = compared["chrome"].get("chain20"), compared["pyv8c"].get("chain20")
    assert ours is not None and ours == theirs, f"pyv8c {ours}; Chrome {theirs}"


async def test_manual_reads_as_an_opaque_redirect_on_both_sides(compared):
    for name, _init, _status in OPAQUE:
        assert compared["pyv8c"][name] == compared["chrome"][name], name


async def test_a_load_chrome_refuses_is_refused_here_too(compared):
    for name, _init, _status in REFUSED:
        assert compared["chrome"][name]["ok"] is False, f"{name}: the reference followed it"
        assert compared["pyv8c"][name]["ok"] is False, f"{name}: pyv8c followed it"


async def test_a_cross_origin_chain_lands_on_the_same_url_with_the_same_body(compared):
    for name, _init, _status in CROSSED:
        theirs = {key: value for key, value in compared["chrome"][name].items()
                  if key not in UNCOMPARED}
        ours = {key: value for key, value in compared["pyv8c"][name].items()
                if key not in UNCOMPARED}
        assert ours == theirs, name


async def test_each_case_makes_the_same_requests_in_the_same_order(compared):
    for name, _init, _status in CASES:
        theirs = [(method, path) for method, path, _auth, _cookie
                  in _for_case(compared["chrome_wire"], name)]
        ours = [(method, path) for method, path, _auth, _cookie
                in _for_case(compared["pyv8c_wire"], name)]
        assert ours == theirs, name


async def test_a_cross_origin_hop_loses_authorization_on_both_sides(compared):
    theirs = [record for record in compared["chrome_wire"] if record[1] == "/end-cors-crossAuth"]
    ours = [record for record in compared["pyv8c_wire"] if record[1] == "/end-cors-crossAuth"]
    assert theirs and ours
    assert theirs[0][2] is None and ours[0][2] is None


async def test_a_cookie_set_on_a_redirect_hop_reaches_the_next_one_on_both_sides(compared):
    theirs = [record for record in compared["chrome_wire"] if record[1] == "/cookie-end"]
    ours = [record for record in compared["pyv8c_wire"] if record[1] == "/cookie-end"]
    assert theirs and ours
    assert "hop=1" in (theirs[0][3] or "") and "hop=1" in (ours[0][3] or "")
    assert compared["pyv8c"]["cookieHop"]["text"] == compared["chrome"]["cookieHop"]["text"]


async def test_a_navigation_counts_its_redirects_the_way_chrome_does(compared):
    assert compared["pyv8c_navigation"] == compared["chrome_navigation"]
