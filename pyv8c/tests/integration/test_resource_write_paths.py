"""The writes Chromium reads as a resource: the property and the attribute alike.

A media element or a link written from script used to never load at all, and a second
URL raced the first instead of replacing it.
"""

import asyncio
import json
import threading
import time

import pytest

from pyv8c.async_api import Browser
from pyv8c.web.fetch.response import Response

PAGE = "https://writes.test/page.html"

IMAGE = "const el = document.createElement('img');"
AUDIO = "const el = document.createElement('audio');"
VIDEO = "const el = document.createElement('video');"
LINK = "const el = document.createElement('link'); el.rel = 'stylesheet';"
APPEND = "document.body.appendChild(el);"

WRITES = (
    ("image property", IMAGE + " el.src = '/image-prop.bin'; " + APPEND, "image-prop.bin"),
    ("image attribute", IMAGE + " el.setAttribute('src', '/image-attr.bin'); " + APPEND, "image-attr.bin"),
    ("audio property", AUDIO + " el.src = '/audio-prop.bin'; " + APPEND, "audio-prop.bin"),
    ("audio attribute", AUDIO + " el.setAttribute('src', '/audio-attr.bin'); " + APPEND, "audio-attr.bin"),
    ("video property", VIDEO + " el.src = '/video-prop.bin'; " + APPEND, "video-prop.bin"),
    ("video attribute", VIDEO + " el.setAttribute('src', '/video-attr.bin'); " + APPEND, "video-attr.bin"),
    ("link property", LINK + " el.href = '/sheet-prop.css'; " + APPEND, "sheet-prop.css"),
    ("link attribute",
     "const el = document.createElement('link'); el.setAttribute('rel', 'stylesheet');"
     " el.setAttribute('href', '/sheet-attr.css'); " + APPEND, "sheet-attr.css"),
    ("link rel after href",
     "const el = document.createElement('link'); el.href = '/sheet-late.css';"
     " " + APPEND + " el.rel = 'stylesheet';", "sheet-late.css"),
)

MARKUP = (
    ('<img src="/image-markup.bin">', "image-markup.bin"),
    ('<audio src="/audio-markup.bin"></audio>', "audio-markup.bin"),
    ('<video src="/video-markup.bin"></video>', "video-markup.bin"),
    ('<link rel="stylesheet" href="/sheet-markup.css">', "sheet-markup.css"),
)


def load_into(asked):
    def load(url, request):
        asked.append(str(url).rsplit("/", 1)[-1])
        return Response("", {"url": str(url), "content-type": "text/plain"})
    return load


async def run_script(script, markup=""):
    """One page, one script, and the paths its loader was asked for."""
    asked = []
    browser = (await Browser(resource_loader=load_into(asked), stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body>" + markup + "<script>" + script + "</script></body>", url=PAGE, wait_until="commit"))
        (await page.run_until_idle(timeout=2))
        return asked
    finally:
        (await browser.close())


@pytest.mark.parametrize("name, script, path", WRITES, ids=[case[0] for case in WRITES])
async def test_a_write_starts_the_load(name, script, path):
    asked = (await run_script(script))
    assert path in asked, (name, asked)


@pytest.mark.parametrize("markup, path", MARKUP, ids=[case[0] for case in MARKUP])
async def test_markup_still_starts_the_load(markup, path):
    asked = (await run_script("", markup))
    assert path in asked, (markup, asked)


async def test_a_new_url_replaces_the_load_in_flight():
    """Chromium reports the URL that won: the dropped load fires no event of its own."""
    gate = threading.Event()
    asked = []

    def load(url, request):
        name = str(url).rsplit("/", 1)[-1]
        asked.append(name)
        if name == "first.png":
            gate.wait(5)
        return Response(b"0123", {"url": str(url), "content-type": "image/png"})

    browser = (await Browser(resource_loader=load, stub_logging="off", threaded_loaders=True).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content(
            "<body><script>"
            " window.__events = [];"
            " const el = document.createElement('img');"
            " el.addEventListener('load', () => __events.push('load'));"
            " el.addEventListener('error', () => __events.push('error'));"
            " el.src = '/first.png'; document.body.appendChild(el); el.src = '/second.png';"
            "</script></body>", url=PAGE, wait_until="commit"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "second.png" not in asked:
            (await page.run_until_idle(timeout=0))
            (await asyncio.sleep(0.01))
        gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            (await page.run_until_idle(timeout=0))
            if json.loads(page.evaluate("JSON.stringify(__events)")):
                break
            (await asyncio.sleep(0.01))
        events = json.loads(page.evaluate("JSON.stringify(__events)"))
        complete = page.evaluate("document.querySelector('img').complete")
    finally:
        (await browser.close())
    assert [name for name in asked if name.endswith(".png")] == ["first.png", "second.png"], asked
    assert events == ["load"], events
    assert complete is True, complete
