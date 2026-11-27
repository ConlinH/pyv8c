"""Offline document-layout fixtures."""

import json

from benchmarks.cases import style


NESTED_READ = """() => {
    const origin = document.querySelector('.grid').getBoundingClientRect();
    return Array.from(document.querySelectorAll('[data-box]'), node => {
        const rect = node.getBoundingClientRect();
        return [rect.x - origin.x, rect.y - origin.y, rect.width, rect.height];
    });
}"""


def nested_markup(*, unequal=False):
    width, tracks = (208, "110px 90px") if unequal else (200, "100px 100px")
    gap = "column-gap:8px" if unequal else ""
    return (
        "<!doctype html><style>"
        f".grid{{display:grid;width:{width}px;grid-template-columns:{tracks};{gap}}}"
        ".row,.column{display:flex;width:90px;height:30px}"
        ".column{flex-direction:column}"
        ".leaf{width:20px;height:10px}.leaf[data-wide]{width:32px}</style>"
        + "".join(
            '<section class="grid" data-box>'
            '<div class="row" data-box><div class="leaf" data-box></div><div class="leaf" data-box></div></div>'
            '<div class="column" data-box><div class="leaf" data-box></div><div class="leaf" data-box></div></div>'
            '</section>' for _ in range(16)
        )
    )


async def prepare(page, case):
    markup = style._markup()
    if case == "replace_block":
        stylesheet, replacement = markup.split("</style>", 1)
        markup = stylesheet + '</style><main id="replacement">' + replacement + '</main>'
        await page.set_content(markup, url="https://style.test/")
        page.evaluate("globalThis.replacementMarkup = " + json.dumps(replacement))
        return """(() => {
            const target = document.getElementById('replacement');
            target.innerHTML = replacementMarkup;
            const children = target.children;
            return [children.length, children[0].offsetWidth, children[children.length - 1].offsetWidth];
        })()""", [128, 100, 227]
    if case != "layout":
        raise ValueError(f"unknown layout fixture: {case}")
    await page.set_content(markup, url="https://style.test/")
    return style._source(case)
