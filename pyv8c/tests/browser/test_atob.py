"""``Window.atob``: the values and the messages Chrome 153 answers with.

Every expectation here was read off a real Chrome over ``page.evaluate``; the decoded
value is compared as code units so the file stays plain ASCII.

Every case only reads, so the module shares one page (``shared_page``).
"""

import json

import pytest

pyv8 = pytest.importorskip("pyv8c.pyv8")

NOT_ENCODED = ("Failed to execute 'atob' on 'Window': The string to be decoded is not"
               " correctly encoded.")
OUTSIDE_LATIN1 = ("Failed to execute 'atob' on 'Window': The string to be decoded contains"
                  " characters outside of the Latin1 range.")

DECODED = (
    ("", []),
    ("YQ==", [97]),
    ("YQ", [97]),
    ("YR", [97]),
    ("ab", [105]),
    ("abc", [105, 183]),
    ("abcd", [105, 183, 29]),
    ("AAA=", [0, 0]),
    ("AAAA", [0, 0, 0]),
    ("/w==", [255]),
    ("a+b/", [107, 230, 255]),
    ("+/+/", [251, 255, 191]),
    ("zzz=", [207, 60]),
    ("a b", [105]),
    (" YQ == ", [97]),
    ("\tYQ==\r\n", [97]),
    ("null", [158, 233, 101]),
    ("89", [243]),
)

REFUSED = (
    "YQ=", "Y", "=", "==", "===", "====", "abcde", "abcd=", "a-b_", "%", "0", "12345",
    "YQ==YQ==", "YQ===", "Y===", "\u00a0YQ==", "\u00e9YQ==", "YQ\u0000==", "\u00ff",
    "Y\u00ff", "\u0080YQ==", "undefined", "k\u00e6\u00ff",
)

REFUSED_AS_OUTSIDE_LATIN1 = (
    "\u2028", "\u2028YQ==", "%\u2028", "\u2028%", "YQ==\u2028", "YQ\u2028==",
    "\ud800", "YQ\ud800==", "\uffff", "\u20ac", "\ufeffYQ==", "\u2029YQ==",
)


def refused(page, text):
    """``[name, code, message]`` for one call, or ``"decoded"`` when it did not throw."""
    return page.evaluate(
        "(() => { try { atob(%s); return 'decoded'; } catch (e) { return [e.name, e.code, e.message]; } })()"
        % json.dumps(text)
    )


@pytest.mark.parametrize("text, coded", DECODED)
async def test_decodes_the_bytes_chrome_decodes(shared_page, text, coded):
    assert shared_page.evaluate(
        "Array.from(atob(%s), (character) => character.charCodeAt(0))" % json.dumps(text)
    ) == coded


@pytest.mark.parametrize("text", REFUSED)
async def test_refuses_what_cannot_be_decoded(shared_page, text):
    assert refused(shared_page, text) == ["InvalidCharacterError", 5, NOT_ENCODED]


@pytest.mark.parametrize("text", REFUSED_AS_OUTSIDE_LATIN1)
async def test_a_code_unit_above_latin1_is_named_as_such(shared_page, text):
    assert refused(shared_page, text) == ["InvalidCharacterError", 5, OUTSIDE_LATIN1]


async def test_a_calling_argument_is_converted_with_the_same_wording(shared_page):
    for text, expected in (
        ("Symbol('x')", ["TypeError", "Failed to execute 'atob' on 'Window':"
                                    " Cannot convert a Symbol value to a string"]),
        ("Object.create(null)", ["TypeError", "Failed to execute 'atob' on 'Window':"
                                             " Cannot convert object to primitive value"]),
        ("{toString: () => { throw new Error('boom'); }}", ["Error", "boom"]),
        ("{toString: () => 'YQ=='}", "decoded"),
        ("{valueOf: () => 'YQ=='}", ["InvalidCharacterError", NOT_ENCODED]),
        ("null", "decoded"),
        ("123", "decoded"),
        ("true", "decoded"),
        ("[89,81]", ["InvalidCharacterError", NOT_ENCODED]),
        ("{}", ["InvalidCharacterError", NOT_ENCODED]),
    ):
        assert shared_page.evaluate(
            "(() => { try { atob(%s); return 'decoded'; } catch (e) { return [e.name, e.message]; } })()"
            % text
        ) == expected, text


async def test_the_argument_count_is_enforced(shared_page):
    assert shared_page.evaluate(
        "(() => { try { atob(); return 'decoded'; } catch (e) { return [e.name, e.message]; } })()"
    ) == ["TypeError", "Failed to execute 'atob' on 'Window': 1 argument required,"
                       " but only 0 present."]
    assert shared_page.evaluate("atob('YQ==', 'ignored')") == "a"


async def test_it_is_a_native_function_on_the_window_itself(shared_page):
    assert shared_page.evaluate("[atob.length, atob.name, String(atob)]") == [
        1, "atob", "function atob() { [native code] }",
    ]
    assert shared_page.evaluate("""
        (() => {
          const descriptor = Object.getOwnPropertyDescriptor(window, 'atob');
          return [window.hasOwnProperty('atob'), 'atob' in Window.prototype,
                  descriptor.writable, descriptor.enumerable, descriptor.configurable];
        })()
    """) == [True, False, True, True, True]


async def test_it_cannot_be_called_as_a_constructor_or_on_another_receiver(shared_page):
    assert shared_page.evaluate(
        "(() => { try { new atob('YQ=='); return 'ok'; } catch (e) { return e.message; } })()"
    ) == "atob is not a constructor"
    assert shared_page.evaluate(
        "(() => { try { atob.call({}, 'YQ=='); return 'ok'; } catch (e) { return e.message; } })()"
    ) == "Illegal invocation"
