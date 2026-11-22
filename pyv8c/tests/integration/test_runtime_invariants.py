import pytest
from pyv8c.pyv8 import IdlSpec

from pyv8c.async_api import Browser
from pyv8c.engine.bindings import Idl, Bindings
from pyv8c.foundation.host_errors import HostStateError
from pyv8c.web.page_errors import DOMException
from pyv8c.platform.sessions import BindingSession
from pyv8c.web.clone import rebuild_clone, structured_clone
from pyv8c.network.cookies import CookieJar


async def new_page(*, loader=None):
    browser = (await Browser(resource_loader=loader).__aenter__())
    context = browser.new_profile()
    page = context.new_page()
    (await page.set_content("<body></body>", url="http://example.test/root", wait_until="commit"))
    (await page.run_until_idle())
    return browser, context, page


async def test_closed_context_rejects_new_pages_and_blob_access():
    browser = (await Browser().__aenter__())
    context = browser.new_profile()
    (await context.close())
    with pytest.raises(HostStateError, match="Profile is closed"):
        context.new_page()
    with pytest.raises(HostStateError, match="Profile is closed"):
        context.resolve_blob_url("blob:null/not-present")
    (await browser.close())


async def test_structured_clone_rejects_cycles_and_nontransferable_values():
    cyclic = []
    cyclic.append(cyclic)
    for value in (cyclic, lambda: None, object()):
        with pytest.raises(DOMException) as caught:
            structured_clone(value)
        assert caught.value.get_name() == "DataCloneError"


async def test_js_values_are_strictly_cloneable_for_worker_messages():
    """A value read out of a realm clones into something a message can carry.

    Binary inside it is held as the view and its bytes until a realm reads it, so a host
    reading the clone itself asks for the bytes -- which is what a realm's read gets when
    there is no realm to build a view in.
    """
    browser, _, page = (await new_page())
    value = page.evaluate("({value:4, bytes:new Uint8Array([1,2,3])})")
    cloned = rebuild_clone(structured_clone(value))
    assert cloned["value"] == 4
    assert bytes(cloned["bytes"]) == b"\x01\x02\x03"
    (await page.close())
    (await browser.close())


async def test_cookie_path_match_uses_path_boundary_and_preserves_repeated_set_cookie():
    jar = CookieJar()
    jar.add_from_response("http://example.test/foo/start", ["first=one; Path=/foo", "root=yes; Path=/"])
    assert jar.header_for("http://example.test/foo/next") == "first=one; root=yes"
    assert jar.header_for("http://example.test/foobar") == "root=yes"

@pytest.mark.parametrize("name", ["Node", "Element", "Window"])
async def test_native_specs_do_not_mutate_interface_declarations(name):
    from pyv8c.platform.bootstrap import bootstrap_runtime

    bindings = Bindings(
        Idl.load(), runtime=bootstrap_runtime(), stub_logging="off",
    )
    interface = bindings.class_for(name)
    # A class no longer has to state anything: what a spec used to restate is the IDL's now.
    declaration = dict(interface.__dict__.get("__idl_overrides__") or {})
    config = BindingSession(bindings, object()).class_config_for(name)
    assert isinstance(config, IdlSpec)
    assert all(callable(member.callback) for member in config.methods)
    assert not hasattr(config, "callback_owner")
    assert (interface.__dict__.get("__idl_overrides__") or {}) == declaration
