"""The redirect table, as observed on the wire from Chrome 154."""
import pytest

from pyv8c.network.redirects import (
    DELIVERED,
    OPAQUE,
    REDIRECT_LIMIT,
    BadLocation,
    RedirectFailure,
    TooManyRedirects,
    is_redirect,
    location_text,
    next_hop,
    reportable_hops,
    resolved_location,
)

HOP = "http://site.test/a"
NEXT = "http://site.test/b"


def hop(status, location=None, method="GET", body=None, headers=None, hops=0, **extra):
    return next_hop(status=status, method=method, url=HOP, headers=headers, body=body,
                    location=location, hops=hops, **extra)


def test_only_the_five_statuses_a_browser_follows_are_redirects():
    assert [status for status in (301, 302, 303, 307, 308) if is_redirect(status)] == [301, 302, 303, 307, 308]
    assert [status for status in (200, 204, 300, 304, 400, 500) if is_redirect(status)] == []
    assert not is_redirect("not a status")


def test_301_and_302_turn_a_post_into_a_get_without_its_body():
    for status in (301, 302):
        answered = hop(status, NEXT, method="POST", body=b"payload")
        assert (answered.method, answered.body) == ("GET", None)


def test_a_method_other_than_post_survives_301_and_302():
    answered = hop(302, NEXT, method="PUT", body=b"payload")
    assert (answered.method, answered.body) == ("PUT", b"payload")


def test_303_turns_every_method_into_a_get_but_leaves_head():
    assert hop(303, NEXT, method="POST", body=b"payload").method == "GET"
    assert hop(303, NEXT, method="DELETE").method == "GET"
    assert hop(303, NEXT, method="HEAD").method == "HEAD"


def test_307_and_308_keep_the_method_and_the_body():
    for status in (307, 308):
        answered = hop(status, NEXT, method="POST", body=b"payload")
        assert (answered.method, answered.body) == ("POST", b"payload")


def test_a_relative_location_resolves_against_the_hop_that_carried_it():
    assert hop(302, "../next").url == "http://site.test/next"
    assert resolved_location("/x", HOP) == "http://site.test/x"
    assert resolved_location("http://other.test/x", HOP) == "http://other.test/x"


def test_a_missing_or_empty_location_is_delivered_as_it_arrived():
    assert hop(302) == DELIVERED
    assert hop(302, "") == DELIVERED
    assert hop(302, "   ") == DELIVERED


def test_an_ordinary_response_is_delivered_even_with_a_location():
    for status in (200, 300, 304):
        assert hop(status, NEXT) == DELIVERED


def test_a_location_that_cannot_be_a_url_fails_the_load():
    for location in ("http://[bad", "mailto:someone@site.test", "data:text/plain,x"):
        with pytest.raises(BadLocation):
            hop(302, location)


def test_the_twenty_first_redirect_fails_and_the_twentieth_does_not():
    assert hop(302, NEXT, hops=REDIRECT_LIMIT - 1).url == NEXT
    with pytest.raises(TooManyRedirects):
        hop(302, NEXT, hops=REDIRECT_LIMIT)
    assert issubclass(TooManyRedirects, RedirectFailure)


def test_a_cross_origin_hop_drops_authorization_and_a_same_origin_one_keeps_it():
    headers = {"Authorization": "Bearer t", "X-Kept": "1"}
    across = hop(302, "http://other.test/b", headers=headers)
    assert across.cross_origin and across.headers == {"X-Kept": "1"}
    along = hop(302, NEXT, headers=headers)
    assert not along.cross_origin and along.headers == headers


def test_the_body_headers_leave_with_the_body():
    headers = {"Content-Type": "text/plain", "Content-Length": "7"}
    assert hop(302, NEXT, method="POST", body=b"payload", headers=headers).headers == {}
    assert hop(307, NEXT, method="POST", body=b"payload", headers=headers).headers == headers


def test_a_manual_redirect_reads_as_opaque_and_error_refuses_it():
    assert hop(302, NEXT, redirect="manual") == OPAQUE
    assert hop(302, NEXT, redirect="manual", mode="no-cors") == OPAQUE
    assert hop(200, redirect="manual") == DELIVERED
    with pytest.raises(RedirectFailure):
        hop(302, NEXT, redirect="error")
    assert hop(200, redirect="error") == DELIVERED


def test_a_navigation_follows_whatever_its_redirect_mode_says():
    assert hop(302, NEXT, mode="navigate", redirect="manual").url == NEXT
    assert hop(302, NEXT, mode="navigate", redirect="error").url == NEXT


def test_a_navigation_reports_no_count_once_a_hop_crossed_origins():
    assert reportable_hops(0, False) == 0
    assert reportable_hops(2, False) == 2
    assert reportable_hops(2, True) == 0


def test_one_location_is_read_however_a_transport_spelled_it():
    assert location_text(["http://site.test/b"]) == "http://site.test/b"
    assert location_text([]) == ""
    assert location_text(None) == ""
    assert resolved_location(["http://site.test/b"], HOP) == "http://site.test/b"
