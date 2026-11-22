"""What the jar keeps of a response's ``Set-Cookie``.

The 2026-10-06 runs against steamdb.info showed the same thing twice: the challenge's submit
response carried ``set-cookie: cf_clearance`` and by the very next exchange the jar held only
``cf_chl_rc_ni`` -- so the header was handed to the jar and one of its cookies was not kept.
Cloudflare sends that answer with attributes a plain parser can trip on: a comma inside the
expiry date, several cookies in one response, and ``SameSite=None``.
"""
import time
from email.utils import formatdate
from urllib.parse import urlsplit

import pytest

pytest.importorskip("pyv8c.pyv8", reason="the engine is imported by the package under test")

from pyv8c.network.cookies import CookieJar  # noqa: E402

URL = "https://steamdb.info/cdn-cgi/challenge-platform/h/b/fo/1234:5678:abcd/ray-1"


def _expires_in_a_year():
    """An RFC 1123 expiry a year out, comma after the weekday and all.

    These were hand-written dates until they arrived: on 2026-10-07 the two tests below started
    failing because their cookies had become past-expiry ones, which the jar drops -- as
    ``test_an_expiry_in_the_past_is_not_kept`` says it should.  The comma inside the date is the
    part the parser has to survive, so a computed one keeps the case and loses the rot.
    ``formatdate`` rather than ``strftime``: its weekday and month names are English whatever
    locale the test runs under.
    """
    return formatdate(time.time() + 365 * 24 * 60 * 60, usegmt=True)


def test_a_cf_shaped_answer_keeps_both_cookies():
    jar = CookieJar()
    jar.add_from_response(URL, (
        "cf_clearance=NGV87Rzpsn_2wjUyFa_KdKinRRT9C8a6EhqeGS3FihM-1791301318-1.2.1.1-"
        "WT1MZjmd16M0bFRfFBqWG1MrJgHSp5T76azGuIHu64kLc4n.YZ6.tndEBPG4pV; path=/; "
        f"domain=.steamdb.info; expires={_expires_in_a_year()}; HttpOnly; Secure; "
        f"SameSite=None, cf_chl_rc_ni=1; expires={_expires_in_a_year()}; path=/; "
        "domain=.steamdb.info; HttpOnly; Secure; SameSite=None"
    ))
    names = sorted(name for (_domain, _path, name) in jar._cookies)
    assert names == ["cf_chl_rc_ni", "cf_clearance"], jar.header_for(URL)


def test_a_single_cookie_reports_the_domain_the_jar_chose():
    jar = CookieJar()
    jar.add_from_response(URL, "cf_clearance=value; path=/; domain=.steamdb.info; "
                               f"expires={_expires_in_a_year()}; Secure")
    assert sorted(jar._cookies) == [("steamdb.info", "/", "cf_clearance")], jar._cookies
    assert "cf_clearance=value" in jar.header_for(URL)


def test_a_cookie_set_for_another_domain_is_refused():
    jar = CookieJar()
    jar.add_from_response(URL, "cf_clearance=value; path=/; domain=.example.com")
    assert jar._cookies == {}
    assert jar.header_for(URL) == ""


def test_an_expiry_in_the_past_is_not_kept():
    jar = CookieJar()
    jar.add_from_response(URL, "cf_clearance=value; path=/; domain=.steamdb.info; "
                               "expires=Thu, 01 Jan 1970 00:00:01 GMT")
    assert jar._cookies == {}


def test_a_cookie_is_only_sent_to_the_path_it_was_given():
    jar = CookieJar()
    jar.add_from_response(URL, "deep=1; path=/cdn-cgi; domain=.steamdb.info")
    assert "deep=1" in jar.header_for(URL)
    assert jar.header_for("https://steamdb.info/") == ""
    assert urlsplit(URL).hostname == "steamdb.info"
