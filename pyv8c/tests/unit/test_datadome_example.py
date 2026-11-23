"""Parameter delivery ends after one submission, regardless of the returned view."""
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytest.importorskip('pyv8c.pyv8')

from pyv8c import NetworkResponse
from pyv8c.network.curl_cffi import CurlCffiTransport


@pytest.fixture(scope='module')
def example():
    path = Path(__file__).resolve().parents[3] / 'examples/datadome/neimanmarcus.py'
    spec = importlib.util.spec_from_file_location('datadome_case_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(body, status=200):
    return NetworkResponse(url='https://geo.captcha-delivery.com/interstitial/', status=status,
                           body=json.dumps(body), headers={'content-type': 'application/json'})


@pytest.mark.parametrize('view', ['redirect', 'captcha', None])
def test_extracts_datadome_value_without_requiring_homepage_access(example, view):
    cookie = 'datadome=fixture~token=value; Domain=.neimanmarcus.com; Path=/'
    assert example.datadome_value(response({'view': view, 'cookie': cookie})) == 'fixture~token=value'


@pytest.mark.parametrize('body', [
    {'view': 'redirect'},
    {'view': 'redirect', 'cookie': 'datadome=; Path=/'},
    {'view': 'redirect', 'cookie': 'other=fixture; Path=/'},
    {'view': 'redirect', 'cookie': 123},
    [],
])
def test_rejects_missing_or_invalid_datadome_value(example, body):
    with pytest.raises(RuntimeError):
        example.datadome_value(response(body))


def test_http_error_cannot_be_overridden_by_redirect_json(example):
    with pytest.raises(RuntimeError, match='HTTP 403'):
        example.datadome_value(response({'view': 'redirect', 'cookie': 'datadome=fixture'}, 403))


def test_html_http_200_has_explicit_response_type_error(example):
    result = NetworkResponse(url='https://geo.captcha-delivery.com/interstitial/',
                             headers={'Content-Type': 'text/html'}, body='<html>Blocked</html>')
    with pytest.raises(RuntimeError, match='Content-Type=text/html'):
        example.datadome_value(result)


@pytest.mark.parametrize('view', ['redirect', 'captcha'])
async def test_run_submits_once_and_never_revisits_homepage(example, monkeypatch, view):
    requests = []
    params = dict.fromkeys(example.REQUIRED, 'fixture')
    params.update(payload='payload+with=encoding~', plv3='plv3/with+encoding=')

    async def transport(self, request):
        requests.append(request)
        if request.url == example.URL:
            assert len(requests) == 1, 'The homepage must not be revisited'
            body = json.dumps(urlencode(params))
            script = "var dd={};fetch('https://geo.captcha-delivery.com/interstitial/'," + (
                "{method:'POST',body:" + body + "}).catch(()=>{});")
            return NetworkResponse(url=request.url, status=403, body=f'<script>{script}</script>',
                                   headers={'content-type': 'text/html'})
        assert request.method == 'POST' and urlsplit(request.url).path == '/interstitial/'
        assert parse_qs(request.body)['payload'] == [params['payload']]
        return response({'view': view, 'cookie': 'datadome=fixture-token; Path=/'})

    monkeypatch.setattr(CurlCffiTransport, '_single_call', transport)
    result = await example.run(proxy=None, timeout=2)
    assert result['payload'] == params['payload']
    assert result['plv3'] == params['plv3']
    assert result['datadome'] == 'fixture-token'
    assert len(requests) == 2


async def test_direct_homepage_without_challenge_is_not_parameter_generation(example, monkeypatch):
    requests = []

    async def transport(self, request):
        requests.append(request)
        return NetworkResponse(url=request.url, body='<main data-reactroot>Home</main>',
                               headers={'content-type': 'text/html'})

    monkeypatch.setattr(CurlCffiTransport, '_single_call', transport)
    with pytest.raises(RuntimeError, match='interstitial'):
        await example.run(proxy=None, timeout=2)
    assert len(requests) == 1


@pytest.mark.parametrize('missing', ['payload', 'plv3'])
async def test_missing_parameter_stops_before_submission(example, monkeypatch, missing):
    requests = []
    params = {name: 'fixture' for name in example.REQUIRED if name != missing}

    async def transport(self, request):
        requests.append(request)
        assert request.url == example.URL and request.method == 'GET'
        script = "var dd={};fetch('https://geo.captcha-delivery.com/interstitial/'," + (
            "{method:'POST',body:" + json.dumps(urlencode(params)) + "}).catch(()=>{});")
        return NetworkResponse(url=request.url, status=403, body=f'<script>{script}</script>',
                               headers={'content-type': 'text/html'})

    monkeypatch.setattr(CurlCffiTransport, '_single_call', transport)
    with pytest.raises(RuntimeError, match=missing):
        await example.run(proxy=None, timeout=2)
    assert len(requests) == 1
