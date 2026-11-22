"""The proxy value, and what each role's options are told about it.

Everything here is plain Python: a proxy is decided before any engine exists --
the value is normalized, then merged into the options of the engines a context
is about to build -- so none of it needs a browser or the native extension.
What each engine then does with the value is the engines' own business, and the
wire-level proof lives in ``tests/integration/test_proxy_routing.py``.
"""
from types import SimpleNamespace

import pytest

from pyv8c.network.assembly import assemble_backend
from pyv8c.network.proxy import (
    PROXY_OPTION,
    curl_proxies,
    normalize_proxy,
    websockets_proxy,
)
from pyv8c.network.registry import accepts_proxy, register_transport, register_ws_transport

#: Registered engines that record the options they were built with, so what the
#: assembly merges in can be read back without building a real client.  The two
#: roles get a name each: one name for both is the shared-engine case, which
#: builds a single instance and is asserted on its own below.
PROBE = "proxy-probe-engine"
PROBE_WS = "proxy-probe-sockets"


class Probe:
    """Just enough of both roles for the assembly to accept it."""

    def __init__(self, role):
        self.role = role

    def submit_request(self, request, handle, remaining):
        raise NotImplementedError

    def websocket(self, tasks, url, protocols, callbacks, options=None):
        raise NotImplementedError

    def close(self):
        return None


@pytest.fixture
def built():
    """The options each role's probe engine was built with, appended in order."""
    made = {"http": [], "ws": []}

    def factory(role):
        def build(*, options=None):
            made[role].append(options)
            return Probe(role)
        return build

    # The keys are the ones an engine of that role could carry, including the
    # proxy keys curl_cffi names -- so a test can configure one of those and see
    # that the assembly leaves it alone.
    keys = frozenset({PROXY_OPTION, "flavour", "proxies", "proxy_auth"})
    register_transport(PROBE, factory("http"), option_keys=lambda: keys, proxy=True)
    register_ws_transport(PROBE_WS, factory("ws"), option_keys=lambda: keys, proxy=True)
    return made


def browser(**overrides):
    """The little of a Runtime the assembly reads."""
    settings = {
        "real_network": True,
        "http_engine": PROBE,
        "http_options": {},
        "ws_engine": PROBE_WS,
        "ws_options": {},
    }
    settings.update(overrides)
    return SimpleNamespace(**settings)


# -- the value ---------------------------------------------------------------


async def test_a_proxy_url_is_kept_with_its_scheme_lowered():
    assert normalize_proxy("https://proxy.test:8080") == "https://proxy.test:8080"
    assert normalize_proxy("HTTP://Proxy.Test:3128") == "http://Proxy.Test:3128"
    assert normalize_proxy("  http://proxy.test:3128  ") == "http://proxy.test:3128"


async def test_credentials_in_the_url_are_kept():
    assert normalize_proxy("http://user:secret@proxy.test:3128") == (
        "http://user:secret@proxy.test:3128"
    )


async def test_an_empty_value_asks_for_a_direct_connection():
    assert normalize_proxy("") == ""
    assert normalize_proxy("   ") == ""


async def test_none_means_nothing_was_configured():
    assert normalize_proxy(None) is None


async def test_a_socks_proxy_is_refused_by_name():
    for value in ("socks5://proxy.test:1080", "socks5h://proxy.test:1080",
                  "socks4://proxy.test:1080"):
        with pytest.raises(ValueError, match="SOCKS"):
            normalize_proxy(value)


async def test_a_value_without_a_scheme_says_what_to_write():
    with pytest.raises(ValueError, match="needs a scheme"):
        normalize_proxy("proxy.test:3128")
    with pytest.raises(ValueError, match="needs a scheme"):
        normalize_proxy("localhost:3128")


async def test_a_proxy_without_a_host_is_refused():
    for value in ("http://", "http://:3128"):
        with pytest.raises(ValueError, match="needs a host"):
            normalize_proxy(value)


async def test_an_unusable_port_is_refused():
    with pytest.raises(ValueError, match="unusable port"):
        normalize_proxy("http://proxy.test:not-a-port")


async def test_a_non_string_proxy_is_a_type_error():
    for value in (8080, object(), ["http://proxy.test:3128"]):
        with pytest.raises(TypeError, match="proxy must be"):
            normalize_proxy(value)


# -- what each engine is handed ----------------------------------------------


async def test_curl_is_given_the_proxy_for_both_schemes():
    assert curl_proxies("http://proxy.test:3128") == {
        "http": "http://proxy.test:3128", "https": "http://proxy.test:3128",
    }
    # The empty value is what pins a request to a direct connection; libcurl
    # would otherwise read HTTP_PROXY out of the environment by itself.
    assert curl_proxies("") == {"http": "", "https": ""}


async def test_curl_is_left_alone_when_nothing_was_configured():
    assert curl_proxies(None) is None


async def test_websockets_is_told_directly_rather_than_left_to_its_default():
    # The client's own default is True, meaning "read the environment".
    assert websockets_proxy("") is None
    assert websockets_proxy(None) is None
    assert websockets_proxy("http://proxy.test:3128") == "http://proxy.test:3128"
    # ``True`` is the client's own way of asking for the environment's proxy,
    # which only an explicit engine option can request.
    assert websockets_proxy(True) is True


async def test_engines_declare_whether_they_can_be_sent_through_a_proxy():
    assert accepts_proxy("curl_cffi", "http") is True
    assert accepts_proxy("websockets", "ws") is True
    assert accepts_proxy("cronet", "http") is True
    assert accepts_proxy("cronet", "ws") is False
    assert accepts_proxy("not-registered", "http") is False
    assert accepts_proxy("curl_cffi", "ws") is False


async def test_registering_an_engine_can_declare_proxy_support():
    def factory(*, options=None):
        return Probe("http")

    register_transport("proxy-probe-undeclared", factory,
                       option_keys=lambda: frozenset({PROXY_OPTION}))
    register_transport("proxy-probe-declared", factory,
                       option_keys=lambda: frozenset({PROXY_OPTION}), proxy=True)
    assert accepts_proxy("proxy-probe-undeclared", "http") is False
    assert accepts_proxy("proxy-probe-declared", "http") is True


# -- what the assembly merges in ---------------------------------------------


async def test_a_configured_proxy_reaches_both_roles(built):
    backend = assemble_backend((browser()), proxy="http://proxy.test:3128")
    assert backend.http is not None and backend.ws is not None
    assert built["http"] == [{PROXY_OPTION: "http://proxy.test:3128"}]
    assert built["ws"] == [{PROXY_OPTION: "http://proxy.test:3128"}]


async def test_an_unset_proxy_is_sent_as_a_direct_connection(built):
    """Not merely omitted: both engines read the environment when left alone."""
    backend = assemble_backend((browser()), proxy=None)
    assert backend.http is not None and backend.ws is not None
    assert built["http"] == [{PROXY_OPTION: ""}]
    assert built["ws"] == [{PROXY_OPTION: ""}]


async def test_an_explicit_direct_connection_is_sent_the_same_way(built):
    assemble_backend((browser()), proxy="")
    assert built["http"] == [{PROXY_OPTION: ""}]


async def test_the_engines_own_options_are_kept_alongside_the_proxy(built):
    assemble_backend((browser(http_options={"flavour": "vanilla"})), proxy="http://proxy.test:1")
    assert built["http"] == [{"flavour": "vanilla", PROXY_OPTION: "http://proxy.test:1"}]


async def test_an_unset_proxy_leaves_an_engines_own_proxy_keys_alone(built):
    """A proxy configured the engine's own way is its business, not this one's."""
    assemble_backend((browser(http_options={"proxies": "http://old.test:1"})), proxy=None)
    assert built["http"] == [{"proxies": "http://old.test:1"}]


async def test_a_proxy_and_an_engines_own_proxy_key_are_refused_together(built):
    for key in ("proxy", "proxies", "proxy_auth"):
        with pytest.raises(TypeError, match="keep one"):
            assemble_backend((browser(http_options={key: "x"})), proxy="http://proxy.test:1")


async def test_an_engine_that_cannot_be_proxied_is_refused(built):
    def factory(*, options=None):
        return Probe("http")

    register_transport("proxy-probe-refusing", factory, option_keys=lambda: frozenset())
    with pytest.raises(TypeError, match="cannot be sent through a proxy"):
        assemble_backend((browser(http_engine="proxy-probe-refusing")), proxy="http://proxy.test:1")


async def test_a_proxy_with_no_engine_to_carry_it_is_refused():
    with pytest.raises(TypeError, match="no network engine"):
        assemble_backend((browser(real_network=False)), proxy="http://proxy.test:1")
    with pytest.raises(TypeError, match="no network engine"):
        assemble_backend((browser(http_engine=None, ws_engine=None)), proxy="http://proxy.test:1")


async def test_a_ready_engine_cannot_be_given_a_proxy(built):
    with pytest.raises(TypeError, match="ready"):
        assemble_backend((browser(http_engine=Probe("http"))), proxy="http://proxy.test:1")


async def test_one_engine_serving_both_roles_is_built_once_with_the_proxy(built):
    assemble_backend((browser(ws_engine=PROBE)), proxy="http://proxy.test:3128")
    assert built["http"] == [{PROXY_OPTION: "http://proxy.test:3128"}]
    assert built["ws"] == []


async def test_additional_role_is_assembled_and_closed(monkeypatch):
    from pyv8c.network import assembly

    class Extra:
        closed = False

        def connect(self):
            pass

        def close(self):
            self.closed = True

    extra = Extra()
    configured = (browser(http_engine=None, ws_engine=None))
    configured.extra_engine = extra
    configured.extra_options = {}
    monkeypatch.setattr(assembly, 'KINDS', (*assembly.KINDS, 'extra'))
    monkeypatch.setitem(assembly.KIND_METHODS, 'extra', 'connect')
    backend = assemble_backend(configured)
    jar = object()
    backend.cookies = jar
    assert backend.engine('extra') is extra
    assert extra.cookies is jar
    backend.close()
    assert extra.closed
    assert backend.engine('extra') is None


async def test_leaving_the_proxy_unset_without_engines_is_not_an_error():
    assert assemble_backend((browser(real_network=False)), proxy=None) is None


# -- the real engines read the value in the shape they need -------------------


async def test_curl_cffi_consumes_the_proxy_rather_than_forwarding_it():
    backend = assemble_backend(
        (browser(http_engine="curl_cffi", ws_engine="websockets")),
        proxy="http://proxy.test:3128",
    )
    try:
        assert backend.http.proxies == {
            "http": "http://proxy.test:3128", "https": "http://proxy.test:3128",
        }
        # Consumed by the bridge: the session is never handed a key it does not
        # know, which is what keeps ``http_options`` validation meaningful.
        assert PROXY_OPTION not in backend.http.options
    finally:
        backend.http.close()


async def test_curl_cffi_is_pinned_to_direct_when_nothing_was_configured():
    backend = assemble_backend(
        (browser(http_engine="curl_cffi", ws_engine="websockets")), proxy=None,
    )
    try:
        assert backend.http.proxies == {"http": "", "https": ""}
    finally:
        backend.http.close()


async def test_websockets_keeps_the_value_to_translate_per_connection():
    backend = assemble_backend(
        (browser(http_engine="curl_cffi", ws_engine="websockets")),
        proxy="http://proxy.test:3128",
    )
    try:
        assert backend.ws.options[PROXY_OPTION] == "http://proxy.test:3128"
        assert backend.ws._connect_kwargs("ws://example.test/socket", None)["proxy"] == (
            "http://proxy.test:3128"
        )
    finally:
        backend.http.close()


async def test_websockets_connects_directly_when_nothing_was_configured():
    backend = assemble_backend(
        (browser(http_engine="curl_cffi", ws_engine="websockets")), proxy=None,
    )
    try:
        assert backend.ws._connect_kwargs("ws://example.test/socket", None)["proxy"] is None
    finally:
        backend.http.close()
