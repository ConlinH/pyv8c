from pyv8c.async_api import Browser
from pyv8c.web.fetch.response import Response
import pytest


@pytest.mark.parametrize('markup,nodes', [
    ('<span style="width:80px""><b>label</b></span>', 7),
    ('<div data-x=unquoted"quote>label</div>', 6),
    ('<div id="broken><span class="inside">label</span>', 6),
    ('<div data-x="a>b" title="\' > \'">label</div>', 6),
    ('text < visibleCount; more text', 5),
])
async def test_tolerant_tag_boundaries_preserve_following_scripts(page, markup, nodes):
    # Chrome-observed recovery: stray quotes are not quoted value delimiters.
    (await page.set_content('<body>' + markup +
                     '<script>globalThis.afterTag = true;</script><i id="end">done</i>',
                     url='https://parser.test/', wait_until="commit"))
    assert page.evaluate('[globalThis.afterTag === true, !!document.getElementById("end"),'
                         'document.scripts.length, document.querySelectorAll("*").length]') == [
                             True, True, 1, nodes]


async def test_tolerant_tag_boundary_keeps_script_strings_out_of_resource_discovery():
    requests = []
    browser = (await Browser(real_network=False, resource_loader=lambda url, request:
                      requests.append(url) or {'body': ''}).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><div data-x=unquoted"quote>label</div>'
                         '<script>globalThis.template = "<img src=\'literal.png\'>";</script>'
                         '<img id="real" src="real.png">', url='https://parser.test/', wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        assert page.evaluate('[document.scripts.length, document.images.length, template]') == [
            1, 1, "<img src='literal.png'>"]
        assert requests == ['https://parser.test/real.png']
    finally:
        (await browser.close())


async def test_parser_blocking_external_script_pauses_and_resumes_parser():
    calls = []
    paused = []

    def loader(url, request):
        calls.append(url)
        # Sampled while the request is outstanding: the parser must still be
        # blocked, so the element after <script> is not in the document yet.
        # Checking here (instead of after set_content returns) keeps the
        # assertion independent of how far the event loop has progressed.
        paused.append(page.evaluate(
            "[document.querySelector('#after') === null, document.readyState]"
        ))
        return Response("window.__order.push('external:' + document.querySelector('#before').id);", {"url": url, "content-type": "text/javascript"})

    browser = (await Browser(resource_loader=loader).__aenter__())
    page = browser.new_profile().new_page()
    page.evaluate("window.__order=[]")
    (await page.set_content("<body><div id='before'></div><script src='/a.js'></script><div id='after'></div></body>", url="http://example.test/", wait_until="commit"))
    assert paused == [[True, "loading"]]
    (await page.run_until_idle())
    assert page.evaluate("[document.querySelector('#after') !== null, __order, document.readyState]") == [True, ["external:before"], "complete"]
    assert calls == ["http://example.test/a.js"]
    (await browser.close())


async def test_async_and_defer_order_and_dynamic_script_default_async():
    def loader(url, request):
        if url.endswith("/defer.js"):
            return Response("window.__order.push('defer')", {"url": url, "content-type": "text/javascript"})
        if url.endswith("/async.js"):
            return Response("window.__order.push('async')", {"url": url, "content-type": "text/javascript"})
        return Response("window.__order.push('dynamic')", {"url": url, "content-type": "text/javascript"})

    browser = (await Browser(resource_loader=loader).__aenter__())
    page = browser.new_profile().new_page()
    page.evaluate("window.__order=[]; window.addEventListener('DOMContentLoaded',()=>__order.push('dcl'))")
    (await page.set_content("<body><script async src='/async.js'></script><script defer src='/defer.js'></script><div id='end'></div></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("const s=document.createElement('script'); s.src='/dynamic.js'; document.body.appendChild(s); __order.push('inserted')")
    (await page.run_until_idle())
    order = page.evaluate("__order")
    assert order.index("defer") < order.index("dcl")
    assert order.index("inserted") < order.index("dynamic")
    (await browser.close())


async def test_an_async_defer_script_does_not_hold_up_the_document():
    """async wins over defer: an asynchronous script is not in the queue the document
    waits for before DOMContentLoaded, so it still reaches complete with its events."""

    def loader(url, request):
        return Response("window.__ran = true;",
                        {"url": url, "content-type": "text/javascript"})

    browser = (await Browser(resource_loader=loader).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("""
        <head><script>
            window.__log = [];
            for (const t of ['DOMContentLoaded', 'load', 'pageshow'])
                addEventListener(t, () => __log.push(t + ':' + document.readyState));
            document.addEventListener('readystatechange',
                () => __log.push('rs:' + document.readyState));
        </script></head>
        <body><script async defer src='/x.js'></script><div>body</div></body>
    """, url="http://example.test/", wait_until="commit"))
    (await page.run_until_idle())
    assert page.evaluate("__log") == [
        "rs:interactive", "DOMContentLoaded:interactive", "rs:complete",
        "load:complete", "pageshow:complete",
    ]
    assert page.evaluate("[window.__ran, document.readyState]") == [True, "complete"]
    (await browser.close())


async def test_a_classic_script_crosses_origins_without_asking_for_cors():
    """A classic script is a no-cors load: a tag served without ``ACAO`` still runs.

    That is what a CDN tag does on a real page.  Only a module script, or one that says
    ``crossorigin``, asks for a CORS answer -- and the page's own ``fetch`` of the same URL
    is still refused, which is the boundary this keeps.
    """
    def loader(url, request):
        return Response("window.__tag = true;",
                        {"url": url, "content-type": "text/javascript"})

    browser = (await Browser(resource_loader=loader).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
    page.evaluate("""
        window.__fetch = null;
        const s = document.createElement('script');
        s.src = 'http://other.test/tag.js';
        document.head.appendChild(s);
        fetch('http://other.test/tag.js').then(
            () => __fetch = 'allowed',
            error => __fetch = error.name);
    """)
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("[window.__tag, window.__fetch]") == [True, "TypeError"]
    (await browser.close())


async def test_a_crossorigin_script_still_asks_for_cors():
    """The ``crossorigin`` attribute is the element asking for a CORS answer."""
    def loader(url, request):
        return Response("window.__tag = true;",
                        {"url": url, "content-type": "text/javascript"})

    browser = (await Browser(resource_loader=loader).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://site.test/", wait_until="commit"))
    page.evaluate("""
        const s = document.createElement('script');
        s.src = 'http://other.test/tag.js';
        s.setAttribute('crossorigin', 'anonymous');
        document.head.appendChild(s);
    """)
    (await page.run_until_idle(timeout=1))
    assert page.evaluate("[window.__tag === undefined, document.readyState]") == [True, "complete"]
    (await browser.close())


async def test_detached_dynamic_script_does_not_fetch_until_inserted():
    calls = []
    def loader(url, request):
        calls.append(url)
        return Response("window.__dynamic = (window.__dynamic || 0) + 1", {"url": url, "content-type": "text/javascript"})
    browser = (await Browser(resource_loader=loader).__aenter__())
    page = browser.new_profile().new_page()
    (await page.set_content("<body></body>", url="http://example.test/", wait_until="commit"))
    page.evaluate("window.__dynamic=0; const s=document.createElement('script'); s.src='/dynamic.js'; window.__detached=s")
    assert calls == []
    page.evaluate("document.body.appendChild(__detached)")
    (await page.run_until_idle())
    assert calls == ["http://example.test/dynamic.js"]
    assert page.evaluate("__dynamic") == 1
    (await browser.close())
