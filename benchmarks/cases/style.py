"""Checked stylesheet and layout workloads, including cache hits and invalidation."""

from __future__ import annotations

from math import ceil

from benchmarks.cases.execution import measure_operations, measure_async_operations

CASE_NAMES = ("layout", "computed", "cached", "universal", "cold", "shadow",
              "parse_first", "mutation_query", "mutation_layout", "color", "color_cached", "color_width")
NODES = 128
RULES = 300
CHECKSUM = sum(100 + i for i in range(NODES))
DOM_ROWS = 1200


async def _dom_query_or_layout(browser, name, iterations):
    page = browser.new_profile().new_page()
    markup = '<main>' + ''.join(
        f'<article id="row{i}" class="row"><span>{i}</span></article>'
        for i in range(DOM_ROWS)
    ) + '</main>'
    if name == "parse_first":
        async def operation():
            return await page.set_content(markup, url="https://dom-bench.test/",
                                          wait_until="domcontentloaded")

        await operation()
        assert page.evaluate("document.getElementsByTagName('article').length") == DOM_ROWS
        result = await measure_async_operations(operation, page, iterations)
        assert page.evaluate("document.getElementsByTagName('article').length") == DOM_ROWS
        expected_nodes = DOM_ROWS
    else:
        expected_nodes = NODES if name == "mutation_layout" else DOM_ROWS
        await page.set_content(_markup() if name == "mutation_layout" else markup,
                               url="https://dom-bench.test/", wait_until="commit")
        if name == "mutation_query":
            page.evaluate("globalThis.target = document.getElementById('row1199')")
            source = """(() => {
                let checksum = 0;
                for (let i = 0; i < 8; i++) {
                    target.id = i % 2 ? 'edited-a' : 'edited-b';
                    target.classList.toggle('picked', i % 2 === 0);
                    const selector = i % 2 ? 'main #edited-a.row' : 'main #edited-b.picked';
                    const matches = document.querySelectorAll(selector);
                    checksum += matches.length + Number(matches[0] === target);
                    checksum += Number(document.getElementById(target.id) === target);
                }
                return checksum;
            })()"""
            expected = 24
        else:
            page.evaluate("globalThis.target = document.querySelector('.item0'); target.id = 'layout-target'")
            source = """(() => {
                target.style.width = '111px';
                const a = document.querySelectorAll('#layout-target.item0')[0].getBoundingClientRect().width;
                target.style.width = '123px';
                const b = document.querySelectorAll('#layout-target.item0')[0].getBoundingClientRect().width;
                return a + b;
            })()"""
            expected = 234
        operation = lambda: page.evaluate(source)
        assert operation() == expected
        result = measure_operations(operation, expected, iterations)
    _add_batch_metrics(result)
    result.update(unit="us/batch", warmup=1, nodes=expected_nodes,
                  detail=f"{name}; offline; {expected_nodes} rows",
                  workload=f"style-{name}-v1:{iterations}:{expected_nodes}")
    return result


def _add_batch_metrics(result):
    samples = sorted(result["latencies_ns"])
    result["p95_us"] = samples[ceil(0.95 * len(samples)) - 1] / 1000
    result["batches_per_second"] = 1_000_000 / result["median"]


def _stylesheet(universal=False, offset=100, colors=False):
    rules = []
    for i in range(RULES):
        selector = f':where([data-r="{i}"])' if universal else f".item{i}"
        color = f";color:rgb({i % 256}, 0, 0)" if colors else ""
        rules.append(f"{selector}{{width:{offset + i}px;height:10px{color}}}")
    return "".join(rules)


def _markup(universal=False, colors=False):
    rows = "".join(f'<div class="item{i}" data-r="{i}"></div>' for i in range(NODES))
    return "<style>" + _stylesheet(universal, colors=colors) + "</style>" + rows


def _source(name):
    if name in ("color", "color_cached", "color_width"):
        read = "const read = () => getComputedStyle(colorTarget).color;"
        if name == "color_cached":
            return "(() => {" + read + "return read();})()", "rgb(0, 0, 0)"
        expected = ["rgb(0, 0, 0)", "rgb(1, 0, 0)"]
        if name == "color_width":
            read = """
                const read = () => {
                    const style = getComputedStyle(colorTarget);
                    return [style.color, style.width];
                };
            """
            expected = [["rgb(0, 0, 0)", "100px"], ["rgb(1, 0, 0)", "101px"]]
        return "(() => {" + read + """
            colorTarget.setAttribute('data-r', '0');
            const a = read();
            colorTarget.setAttribute('data-r', '1');
            return [a, read()];
        })()""", expected
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
        if name in ("parse_first", "mutation_query", "mutation_layout"):
            return await _dom_query_or_layout(browser, name, iterations)
        page = browser.new_profile().new_page()
        colors = name in ("color", "color_cached", "color_width")
        markup = _markup(name == "universal" or colors, colors=colors)
        (await page.set_content(markup, url="https://style.test/"))
        if colors:
            page.evaluate("globalThis.colorTarget = document.querySelector('div[data-r]')")
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
        _add_batch_metrics(result)
        result.update(
            unit="us/batch",
            workload=f"style-{name}-v1:{iterations}:{NODES}:{RULES}",
            detail=f"{name}; {NODES} elements, {RULES} rules; checked {'colors and widths' if name == 'color_width' else 'colors' if colors else 'widths'}",
            warmup=3,
            nodes=NODES,
            rules=RULES,
        )
        return result
    finally:
        (await browser.close())
