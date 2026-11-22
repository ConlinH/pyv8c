"""The Event Timing map has Chrome's fixed keys and counts trusted input."""

from pyv8c.async_api import Browser


async def test_event_counts_map_and_trusted_click():
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<button id="b">x</button>', url="https://example.test/", wait_until="commit"))
        assert page.evaluate("""(() => {
            const counts = performance.eventCounts;
            const callback = [];
            counts.forEach((value, name, owner) => {
                if (callback.length < 2) callback.push([value, name, owner === counts]);
            });
            return [counts === performance.eventCounts, counts.size,
                    Object.prototype.toString.call(counts),
                    [...counts.keys()].slice(0, 3), [...counts.entries()].length,
                    [...counts.values()].length, callback,
                    counts.get('click'), counts.has('click'),
                    counts.get('not-an-event') === undefined];
        })()""") == [True, 36, "[object EventCounts]",
                    ["pointerdown", "touchend", "input"], 36, 36,
                    [[0, "pointerdown", True], [0, "touchend", True]], 0, True, True]
        page.evaluate("document.querySelector('#b').dispatchEvent(new MouseEvent('click'))")
        assert page.evaluate("performance.eventCounts.get('click')") == 0
        page.evaluate("window.__eventCountIterator = performance.eventCounts.entries()")
        (await page.click('#b'))
        assert page.evaluate("""['click', 'pointerdown', 'pointerup', 'mousedown', 'mouseup']
            .map(name => performance.eventCounts.get(name))""") == [1, 1, 1, 1, 1]
        assert page.evaluate("""[...window.__eventCountIterator]
            .find(([name]) => name === 'click')""") == ["click", 1]
    finally:
        (await browser.close())
