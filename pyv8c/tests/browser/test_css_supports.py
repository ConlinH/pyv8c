"""CSS.supports answers a condition the way Blink's CSSSupportsParser does."""

import json

from pyv8c.async_api import Browser

CASES = {
    # <supports-decl> and the wrapping parenthesis ParseSupportsCondition adds.
    "display: grid": True,
    "not-a-property: x": False,
    "(display: grid)": True,
    "((display: grid))": True,
    "--x: y": True,
    "(--x: y)": True,
    # <supports-condition>: not / and / or.
    "(display: grid) and (color: red)": True,
    "(display: grid) and (color: red) and (color: blue)": True,
    "(display: grid) or (foo: bar)": True,
    "not (display: grid)": False,
    "not (foo: bar)": True,
    "(display: grid) and (not (display: inline-grid))": False,
    "(foo: bar)": False,
    "(display: grid) and": False,
    "and (display: grid)": False,
    # A function token is not the keyword: `or(` and `not(` do not combine.
    "(display:grid)or(color:red)": False,
    "(display:grid)and(color:red)": False,
    "not(display: grid)": False,
    # <supports-selector-fn>.
    "selector(a)": True,
    "selector(a:hover)": True,
    "selector(:has(a))": True,
    "selector(:is(a, b))": True,
    "selector(:where(a))": True,
    "selector(:not(a))": True,
    "selector(::before)": True,
    "selector(a > b)": True,
    "selector(a b)": True,
    "selector(a + b)": True,
    "selector(a ~ b)": True,
    "selector(*)": True,
    "selector(#a)": True,
    "selector(.a)": True,
    "selector([x=y])": True,
    "selector([x=\"y\"])": True,
    "selector([x^=\"y\"])": True,
    "selector(:nth-child(2n+1))": True,
    "selector(a.b#c)": True,
    "selector(a >)": False,
    "selector(> a)": False,
    "selector(a > > b)": False,
    "selector()": False,
    "selector(!!)": False,
    "selector(a,,b)": False,
    "selector([x=1])": False,
    "selector(1a)": False,
    "selector(:foo)": False,
    "selector(::foo)": False,
    "selector(a#b.c[d]:e)": False,
    # <supports-font-format-fn>, <supports-font-tech-fn>, <supports-at-rule-fn>.
    "font-format(woff2)": True,
    "font-format(collection)": True,
    "font-format(embedded-opentype)": False,
    "font-format(nope)": False,
    "font-tech(color-COLRv1)": True,
    "font-tech(features-opentype)": True,
    "font-tech(features-graphite)": False,
    "font-tech(nope)": False,
    "at-rule(@media)": True,
    "at-rule(@layer)": True,
    "at-rule(@charset)": False,
    "at-rule(@nope)": False,
}


async def test_supports_conditions_match_chrome():
    browser = (await Browser(cache=False, real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        script = """(() => {
            const out = {};
            for (const text of %s) {
              try { out[text] = CSS.supports(text); }
              catch (error) { out[text] = 'THREW:' + error.name; }
            }
            return out;
        })()""" % json.dumps(list(CASES))
        assert page.evaluate(script) == CASES
    finally:
        (await browser.close())
