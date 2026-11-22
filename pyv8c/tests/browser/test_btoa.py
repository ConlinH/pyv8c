"""``Window.btoa``: the values and the messages Chrome 153 answers with.

Every expectation here was read off a real Chrome over ``page.evaluate``.

Every case only reads, so the module shares one page (``shared_page``).
"""

import json

import pytest

pyv8 = pytest.importorskip("pyv8c.pyv8")

OUTSIDE_LATIN1 = ("Failed to execute 'btoa' on 'Window': The string to be encoded contains"
                  " characters outside of the Latin1 range.")

ENCODED = (
    ("", ""),
    ("a", "YQ=="),
    ("ab", "YWI="),
    ("abc", "YWJj"),
    ("abcd", "YWJjZA=="),
    ("abcde", "YWJjZGU="),
    ("hello", "aGVsbG8="),
    ("YQ==", "WVE9PQ=="),
    ("a b", "YSBi"),
    ("a\nb", "YQpi"),
    ("%", "JQ=="),
    ("+/", "Ky8="),
    ("\u0000", "AA=="),
    ("\u007f", "fw=="),
    ("\u0080", "gA=="),
    ("\u00e9", "6Q=="),
    ("\u00ff", "/w=="),
    ("\u00ff\u00ff", "//8="),
    ("\u00ff" * 5, "//////8="),
    ("k\u00e6\u00ff", "a+b/"),
)

REFUSED = ("\u0100", "\u2028", "\ud800", "\u20ac", "\uffff", "\u00ff\u0100")


def called(page, expression):
    """``[name, message]`` for one call, or ``["encoded", value]`` when it did not throw."""
    return page.evaluate(
        "(() => { try { return ['encoded', String(btoa(%s))]; }"
        " catch (e) { return [e.name, e.message]; } })()" % expression
    )


@pytest.mark.parametrize("text, encoded", ENCODED)
async def test_encodes_the_way_chrome_encodes(shared_page, text, encoded):
    assert shared_page.evaluate("btoa(%s)" % json.dumps(text)) == encoded


@pytest.mark.parametrize("text", REFUSED)
async def test_refuses_a_code_unit_above_latin1(shared_page, text):
    assert shared_page.evaluate(
        "(() => { try { btoa(%s); return 'encoded'; } catch (e) { return [e.name, e.code, e.message]; } })()"
        % json.dumps(text)
    ) == ["InvalidCharacterError", 5, OUTSIDE_LATIN1]


async def test_a_latin1_string_survives_a_round_trip(shared_page):
    for text in ("", "a", "\u0000\u007f\u0080\u00ff", "k\u00e6\u00ff", "YQ=="):
        assert shared_page.evaluate("atob(btoa(%s))" % json.dumps(text)) == text


async def test_a_calling_argument_is_converted_with_the_same_wording(shared_page):
    for expression, expected in (
        ("undefined", ["encoded", "dW5kZWZpbmVk"]),
        ("null", ["encoded", "bnVsbA=="]),
        ("123", ["encoded", "MTIz"]),
        ("true", ["encoded", "dHJ1ZQ=="]),
        ("{toString: () => 'a'}", ["encoded", "YQ=="]),
        ("Symbol('x')", ["TypeError", "Failed to execute 'btoa' on 'Window':"
                                    " Cannot convert a Symbol value to a string"]),
        ("Object.create(null)", ["TypeError", "Failed to execute 'btoa' on 'Window':"
                                             " Cannot convert object to primitive value"]),
        ("{toString: () => { throw new Error('boom'); }}", ["Error", "boom"]),
    ):
        assert called(shared_page, expression) == expected, expression


async def test_the_argument_count_is_enforced(shared_page):
    assert shared_page.evaluate(
        "(() => { try { btoa(); return 'encoded'; } catch (e) { return [e.name, e.message]; } })()"
    ) == ["TypeError", "Failed to execute 'btoa' on 'Window': 1 argument required,"
                       " but only 0 present."]
    assert shared_page.evaluate("btoa('a', 'ignored')") == "YQ=="


async def test_it_is_a_native_function_on_the_window_itself(shared_page):
    assert shared_page.evaluate("[btoa.length, btoa.name, String(btoa)]") == [
        1, "btoa", "function btoa() { [native code] }",
    ]
    assert shared_page.evaluate("""
        (() => {
          const descriptor = Object.getOwnPropertyDescriptor(window, 'btoa');
          return [window.hasOwnProperty('btoa'), 'btoa' in Window.prototype,
                  descriptor.writable, descriptor.enumerable, descriptor.configurable];
        })()
    """) == [True, False, True, True, True]


async def test_it_cannot_be_called_as_a_constructor_or_on_another_receiver(shared_page):
    assert shared_page.evaluate(
        "(() => { try { new btoa('a'); return 'ok'; } catch (e) { return e.message; } })()"
    ) == "btoa is not a constructor"
    assert shared_page.evaluate(
        "(() => { try { btoa.call({}, 'a'); return 'ok'; } catch (e) { return e.message; } })()"
    ) == "Illegal invocation"
