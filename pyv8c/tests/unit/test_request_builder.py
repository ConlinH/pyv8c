"""A transport request is finalised once, before any engine sees it."""
from dataclasses import FrozenInstanceError, replace

import pytest

from pyv8c.network.cookies import CookieJar
from pyv8c.network.models import with_cookie_header
from pyv8c.network.request_builder import finalize_request

URL = "https://example.test/path"


def _jar():
    jar = CookieJar()
    jar.add_from_response(URL, "session=1; Path=/")
    return jar


def test_host_and_resource_paths_share_one_cookie_baseline():
    jar = _jar()
    host = finalize_request(url=URL, credentials="include", jar=jar)
    resource = finalize_request(url=URL, credentials="include", origin="https://example.test",
                                site_for_cookies="https://example.test", jar=jar)
    assert host.headers["Cookie"] == resource.headers["Cookie"] == "session=1"


def test_host_cookie_override_leads_and_replaces_same_named_jar_cookie():
    request = finalize_request(url=URL, credentials="include", jar=_jar(),
                               cookie_override={"session": "override", "extra": "2"})
    assert request.headers["Cookie"] == "session=override; extra=2"


def test_omitted_credentials_and_missing_jar_add_no_cookie():
    assert "Cookie" not in finalize_request(url=URL, credentials="omit", jar=_jar()).headers
    assert "Cookie" not in finalize_request(url=URL, credentials="include").headers


def test_an_explicit_override_survives_a_missing_jar():
    request = finalize_request(url=URL, credentials="include",
                               cookie_override={"session": "override", "extra": "2"})
    assert request.headers["Cookie"] == "session=override; extra=2"


def test_an_explicit_cookie_header_is_never_overwritten():
    request = finalize_request(url=URL, credentials="include", headers={"Cookie": "manual=1"}, jar=_jar())
    assert request.headers["Cookie"] == "manual=1"


def test_allow_redirects_option_is_promoted_to_the_field():
    request = finalize_request(url=URL, options={"allow_redirects": False, "impersonate": "chrome"})
    assert request.allow_redirects is False
    assert request.options == {"impersonate": "chrome"}
    assert finalize_request(url=URL).allow_redirects is None


def test_frozen_request_expresses_the_queue_budget_with_replace():
    request = finalize_request(url=URL, timeout=5)
    with pytest.raises(FrozenInstanceError):
        request.timeout = 1
    queued = replace(request, timeout=2)
    assert request.timeout == 5
    assert queued.timeout == 2
    assert queued.url == URL
    assert queued.allow_redirects is None


def test_finalising_copies_the_headers_and_options_it_was_given():
    headers, options = {"X-Test": "1"}, {"verify": False}
    request = finalize_request(url=URL, headers=headers, options=options)
    headers["X-Test"] = "2"
    options["verify"] = True
    assert request.headers == {"X-Test": "1"}
    assert request.options == {"verify": False}


def test_an_engine_cannot_recompute_an_already_finalised_cookie():
    """Preparing the first hop preserves the header chosen by the caller."""
    jar = _jar()
    request = finalize_request(url=URL, credentials="include", jar=jar)
    other = CookieJar()
    other.add_from_response(URL, "other=2; Path=/")
    assert with_cookie_header(request, jar) is request
    assert with_cookie_header(request, other) is request
    assert request.headers["Cookie"] == "session=1"
