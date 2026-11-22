"""Checked stylesheet and layout workloads, including cache hits and invalidation."""

from __future__ import annotations

from benchmarks.cases.execution import measure_operations, measure_async_operations

CASE_NAMES = ("layout", "computed", "cached", "universal", "cold", "shadow")
NODES = 128
RULES = 300
CHECKSUM = sum(100 + i for i in range(NODES))


def _stylesheet(universal=False, offset=100):
    rules = []
    for i in range(RULES):
        selector = f':where([data-r="{i}"])' if universal else f".item{i}"
        rules.append(f"{selector}{{width:{offset + i}px;height:10px}}")
    return "".join(rules)


def _markup(universal=False):
    rows = "".join(f'<div class="item{i}" data-r="{i}"></div>' for i in range(NODES))
    return "<style>" + _stylesheet(universal) + "</style>" + rows


def _source(name):
    root = "document.getElementById('host').shadowRoot" if name == "shadow" else "document"
    read = (
        "parseFloat(getComputedStyle(node).width)"
        if name == "computed"
        else "node.getBoundingClientRect().width"
    )
    setup = f"const nodes=[...{root}.querySelectorAll('div[data-r]')];"
    reader = f"const read=()=>{{let sum=0;for(const node of nodes)sum+={read};return sum}};"
    if name == "cached":
        return "(()=>{" + setup + reader + "return read()})()", CHECKSUM
    body = """
        nodes[0].className='item0';nodes[0].setAttribute('data-r','0');const a=read();
        nodes[0].className='item1';nodes[0].setAttribute('data-r','1');const b=read();
    """
    check = (
        f"if(a!=={CHECKSUM}||b!=={CHECKSUM+1})throw Error('stale or incorrect layout');return a+b"
    )
    return "(()=>{" + setup + reader + body + check + "})()", CHECKSUM * 2 + 1


async def run_case(name, iterations=12):
    if name not in CASE_NAMES or iterations < 1:
        raise ValueError("unknown workload or nonpositive operation count")
    from pyv8c.foundation.logging import logger

    from pyv8c.async_api import Browser

    logger.remove()
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        markup = _markup(name == "universal")
        (await page.set_content(markup, url="https://style.test/"))
        if name == "shadow":
            import json

            (await page.set_content("<style>" + _stylesheet(offset=900) + "</style><div id='host'></div>"))
            page.evaluate(
                "document.getElementById('host').attachShadow({mode:'open'}).innerHTML="
                + json.dumps(markup)
            )
        source, expected = _source("cached" if name == "cold" else name)
        if name == "cold":

            async def operation():
                (await page.set_content(markup, url="https://style.test/"))
                return page.evaluate(source)

        else:
            operation = lambda: page.evaluate(source)
        for _ in range(3):
            assert (await operation() if name == "cold" else operation()) == expected
        result = (await measure_async_operations(operation, expected, iterations) if name == "cold"
                  else measure_operations(operation, expected, iterations))
        result.update(
            unit="us/batch",
            workload=f"style-{name}-v1:{iterations}:{NODES}:{RULES}",
            detail=f"{name}; {NODES} elements, {RULES} rules; checked widths",
            warmup=3,
            nodes=NODES,
            rules=RULES,
        )
        return result
    finally:
        (await browser.close())
