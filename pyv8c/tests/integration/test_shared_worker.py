"""A ``SharedWorker`` hands out a port and answers on it."""
from pyv8c.async_api import Browser
from pyv8c.web.fetch.response import Response

WORKER = ("self.onconnect = (event) => { const port = event.ports[0];"
          " port.postMessage('connected');"
          " port.onmessage = (event) => port.postMessage('echo:' + event.data); };")
PAGE = "<body></body>"


def _loader(url, request):
    if url.endswith("/worker.js"):
        return Response(WORKER, {"url": url, "content-type": "text/javascript"})
    return Response(PAGE, {"url": url, "content-type": "text/html"})


async def test_a_shared_worker_answers_on_the_port_it_hands_out():
    browser = (await Browser(resource_loader=_loader, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(PAGE, url="https://repro.test/index.html", wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        page.evaluate("""window.__seen = [];
            const worker = new SharedWorker('/worker.js', {name: 'pulse'});
            window.__port = worker.port;
            worker.port.onmessage = (event) => window.__seen.push(event.data);
            worker.port.postMessage('hi');""")
        (await page.wait_for("window.__seen.length >= 2", timeout=10))
        seen = page.evaluate("window.__seen")
        shape = page.evaluate(
            "[typeof SharedWorker, SharedWorker.length,"
            " window.__port instanceof MessagePort, typeof window.__port.start]")
    finally:
        (await browser.close())

    assert seen == ["connected", "echo:hi"]
    assert shape == ["function", 1, True, "function"]
