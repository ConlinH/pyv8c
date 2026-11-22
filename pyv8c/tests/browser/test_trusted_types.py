"""Trusted Types: the wrapper a policy hands back, and what ``eval`` does with it.

A ``TrustedScript`` is marked code-like in the binding, so ``eval`` compiles the source it
carries *inside* the native eval -- which is the only way to keep direct-eval scope. The
tests below pin both halves: the source runs, and everything else keeps its old behavior.
"""

import pytest

pytest.importorskip("pyv8c.pyv8")

POLICY = """
window.__policy = trustedTypes.createPolicy('t', {
  createHTML: function (value) { return String(value); },
  createScript: function (value) { return String(value); },
  createScriptURL: function (value) { return String(value); },
});
"""


@pytest.fixture
async def secure_page(page):
    """A secure-context page, where ``trustedTypes`` is exposed at all."""
    (await page.set_content("<body></body>", url="https://probe.test/trusted.html", wait_until="commit"))
    return page


async def test_a_trusted_script_keeps_its_wrapper_shape(secure_page):
    assert secure_page.evaluate(POLICY + """
(() => {
  const script = __policy.createScript('1 + 1');
  return [typeof script, Object.prototype.toString.call(script), String(script),
          trustedTypes.isScript(script), script instanceof TrustedScript];
})()
""") == ["object", "[object TrustedScript]", "1 + 1", True, True]


async def test_eval_runs_the_source_a_policy_produced(secure_page):
    assert secure_page.evaluate(POLICY + """
(() => {
  window.__ran = 0;
  eval(__policy.createScript('window.__ran = 41; 42'));
  return [window.__ran, eval(__policy.createScript('1 + 1'))];
})()
""") == [41, 2]


async def test_eval_of_a_trusted_script_keeps_direct_eval_scope(secure_page):
    """The unwrapping happens inside the native eval, so a ``var`` stays local."""
    assert secure_page.evaluate(POLICY + """
(() => {
  eval(__policy.createScript('var __local = 7'));
  return [typeof __local, typeof window.__local];
})()
""") == ["number", "undefined"]


async def test_eval_through_the_window_property_reaches_the_global_scope(secure_page):
    assert secure_page.evaluate(POLICY + """
(() => {
  window.eval(__policy.createScript('var __global = 9'));
  return [typeof __global, window.__global];
})()
""") == ["number", 9]


async def test_eval_leaves_other_arguments_alone(secure_page):
    assert secure_page.evaluate("""
(() => {
  const object = {a: 1};
  return [eval(object) === object, typeof eval(new String('1 + 1')), eval(7)];
})()
""") == [True, "object", 7]


async def test_the_function_constructor_accepts_a_trusted_script(secure_page):
    assert secure_page.evaluate(POLICY
                                + "Function(__policy.createScript('return 5'))()") == 5


async def test_a_trusted_html_is_not_code(secure_page):
    """Only a script is code-like: an HTML wrapper still comes back untouched."""
    assert secure_page.evaluate(POLICY + """
(() => {
  const html = __policy.createHTML('<b>x</b>');
  return [eval(html) === html, String(html)];
})()
""") == [True, "<b>x</b>"]
