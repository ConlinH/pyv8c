"""The real success chain submits a login document using POST after the challenge."""
from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip('pyv8c.pyv8')

from pyv8c.network.models import NetworkRequest, NetworkResponse

_path = Path(__file__).resolve().parents[3] / 'examples' / '5s' / '5s.py'
_spec = importlib.util.spec_from_file_location('steamdb_login_example', _path)
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)

HTML = '''<title>Sign In · SteamDB</title>
<p>Logging into SteamDB allows you to access additional features.</p>
<p>We will fetch public information about your account.</p>
<p>This website is not affiliated with Valve Corporation or Steam.</p>
<p>We use cookies to keep your session active.</p>
<p>SteamDB never receives your username or password.</p>
<button>Sign in through Steam</button>'''
REQUEST = NetworkRequest(example.URL, mode='navigate', top_level_navigation=True)
RESPONSE = NetworkResponse(example.URL, headers={'content-type': 'text/html'}, body=HTML)


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_matching_document_navigation_is_accepted(method):
    result = example.inspect_login_response(replace(REQUEST, method=method), RESPONSE)
    assert result['ok'] is True
    assert result['method'] == method
    assert result['document']['title'] == 'Sign In · SteamDB'


@pytest.mark.parametrize('changes', [
    {'method': 'DELETE'}, {'method': 'HEAD'},
    {'mode': 'cors'}, {'top_level_navigation': False},
])
def test_non_document_or_unrelated_methods_are_ignored(changes):
    assert example.inspect_login_response(replace(REQUEST, **changes), RESPONSE) is None


@pytest.mark.parametrize('url', [
    'https://steamdb.info/cdn-cgi/challenge-platform/flow',
    'https://example.test/login/', 'http://steamdb.info/login/',
])
def test_wrong_response_destination_is_ignored(url):
    assert example.inspect_login_response(REQUEST, replace(RESPONSE, url=url)) is None


@pytest.mark.parametrize('changes', [
    {'status': 403}, {'body': '<title>Just a moment...</title>'},
    {'headers': {'content-type': 'text/plain'}},
    {'body': HTML.replace('SteamDB never receives', 'Missing password notice')},
])
def test_failed_or_incomplete_document_cannot_pass(changes):
    assert example.inspect_login_response(REQUEST, replace(RESPONSE, **changes))['ok'] is False
