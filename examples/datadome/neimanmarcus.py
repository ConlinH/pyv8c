"""Generate fresh DataDome interstitial parameters, submit once, verify the homepage."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from threading import Event
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger

from pyv8c.async_api import Browser
from pyv8c.platform.resources.request_headers import browser_headers

URL = 'https://www.neimanmarcus.com/'
CHALLENGE_HOSTS = {'ct.captcha-delivery.com', 'geo.captcha-delivery.com',
                   'static.captcha-delivery.com'}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


async def run(proxy=None, output=None, timeout=60):
    """生成一次 interstitial 参数并提交，返回带状态与产物路径的 state。"""
    output = Path(output) if output else Path(__file__).with_name('output') / (
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    ready = Event()
    state = {'success': False, 'target': URL, 'sdk': {}, 'flow': []}
    captured = {}
    phase = 'generating'
    page = None
    started = time.monotonic()

    def note(method, url, status):
        state['flow'].append({'method': method, 'url': url, 'status': status,
                              'elapsed_ms': round((time.monotonic() - started) * 1000)})
        parts = urlsplit(url)
        logger.info('{} {}{} -> {}', method, parts.hostname, parts.path, status)

    def intercept(url, request):
        nonlocal phase
        parts = urlsplit(url)
        if phase != 'generating':
            return False
        if parts.scheme in ('blob', 'data'):
            return None
        if not (parts.hostname in CHALLENGE_HOSTS or
                (parts.hostname == 'www.neimanmarcus.com' and parts.path == '/')):
            return False
        method = request.get('method', 'GET').upper()
        if method == 'POST' and parts.hostname == 'geo.captcha-delivery.com' and parts.path == '/interstitial/':
            body = request.get('body') or ''
            if isinstance(body, bytes):
                body = body.decode('utf-8')
            params = parse_qs(body, keep_blank_values=True)
            required = ('cid', 'hash', 'referer', 'url', 's', 'e', 'env', 'userEnv',
                        'seed', 'b', 'dm', 'payload', 'plv3', 'ps')
            missing = [key for key in required if len(params.get(key, [])) != 1 or not params[key][0]]
            logger.debug('captured interstitial body: {} chars', len(body))
            captured.update(url=url, body=body, headers=dict(request.get('headers') or {}), params=params)
            if missing:
                captured['error'] = 'The SDK did not generate: ' + ', '.join(missing)
            phase = 'captured'
            ready.set()
            # Pause at the outgoing boundary. The host submits these exact bytes
            # once below; this local refusal is never sent to the website.
            return {'status': 400, 'headers': {'content-type': 'application/json'},
                    'body': '{"error":"request captured for host submission"}'}
        if parts.path.startswith('/captcha/'):
            captured['error'] = 'The site switched from the interstitial to a captcha.'
            ready.set()
            return False
        response = page.profile.network_backend.request_sync(page.net.build(
            method, url, headers=request.get('headers'),
            data=request.get('body'), timeout=min(timeout, 25)))
        note(method, url, response.status)
        if parts.hostname == 'ct.captcha-delivery.com':
            state['sdk'][url] = {'bytes': len(response.content),
                                  'sha256': hashlib.sha256(response.content).hexdigest()}
        if parts.hostname == 'geo.captcha-delivery.com' and parts.path == '/interstitial/':
            (output / 'challenge.html').write_bytes(response.content)
        if parts.hostname == 'www.neimanmarcus.com':
            state['initial_status'] = response.status
            (output / 'initial.html').write_bytes(response.content)
            if response.status == 200:
                captured['already_accessible'] = response
                phase = 'captured'
                ready.set()
                return {'status': 200, 'headers': {'content-type': 'text/html'},
                        'body': '<!doctype html><title>Initial response captured</title>'}
            if response.status != 403 or not re.search(r'\bvar\s+dd\s*=', response.text):
                captured['error'] = f'Expected the dd interstitial bootstrap, received HTTP {response.status}.'
                ready.set()
        return {'status': response.status, 'headers': response.headers, 'body': response.body}

    try:
        async with Browser(
            proxy=proxy, cache=False, resource_interceptor=intercept,
            http_options={'impersonate': 'chrome150', 'timeout': min(timeout, 25)},
            stub_logging='off',
        ) as browser:
            context = browser.new_profile()
            page = context.new_page()
            await page.goto(URL, timeout=min(timeout, 60), wait_until='domcontentloaded')
            while not ready.is_set():
                await asyncio.sleep(.1)
                if time.monotonic() - started >= timeout:
                    raise TimeoutError('The local SDK did not generate the interstitial request before timeout.')
            if captured.get('error'):
                raise RuntimeError(captured['error'])
            if 'already_accessible' in captured:
                response = captured['already_accessible']
                state['generated_parameters'] = False
            else:
                params = captured['params']
                write_json(output / 'parameters.json', {key: values[0] for key, values in params.items()})
                (output / 'request.form').write_text(captured['body'], encoding='utf-8')
                state.update(generated_parameters=True, payload_chars=len(params['payload'][0]),
                             plv3_chars=len(params['plv3'][0]))
                logger.info('Generated payload={} chars, plv3={} chars',
                            state['payload_chars'], state['plv3_chars'])
                phase = 'submitting'
                response = await page.net.request(
                    'POST', captured['url'], data=captured['body'],
                    headers=captured['headers'], timeout=min(timeout, 25))
                note('POST', captured['url'], response.status)
                state['interstitial_status'] = response.status
                data = response.json()
                write_json(output / 'interstitial-response.json', data)
                state['view'] = data.get('view') if isinstance(data, dict) else None
                if response.status != 200 or not isinstance(data, dict) or data.get('view') != 'redirect':
                    raise RuntimeError(f'Interstitial rejected: HTTP {response.status}, view={state["view"]!r}.')
                assignment = data.get('cookie', '')
                if not isinstance(assignment, str) or not assignment.startswith('datadome='):
                    raise RuntimeError('The redirect response contains no datadome cookie.')
                cookie = assignment.split(';', 1)[0]
                context.cookies.set_from_document(URL, assignment)
                if cookie not in context.cookies.header_for(URL).split('; '):
                    raise RuntimeError('The returned cookie was not stored for the target domain.')
                headers = browser_headers(page.environment, destination='document', mode='navigate',
                                          method='GET', url=URL, current_url=URL, http_version=2)
                response = await page.net.request(
                    'GET', URL, headers=headers, referer=URL, timeout=min(timeout, 25))
                note('GET', URL, response.status)
            state.update(home_status=response.status, html_bytes=len(response.content))
            (output / 'home.html').write_bytes(response.content)
            if response.status != 200 or 'data-reactroot' not in response.text:
                raise RuntimeError(f'Homepage verification failed: HTTP {response.status}.')
            state['success'] = True
            logger.info('Homepage verified: HTTP 200, {} bytes; artifacts: {}', len(response.content), output)
            return state
    except Exception as exc:
        state['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        phase = 'closed'
        state['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        write_json(output / 'result.json', state)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default='http://127.0.0.1:7890')
    parser.add_argument('--output-dir', type=Path, help='A new directory for this run\'s private artifacts.')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    logger.remove()
    logger.add(sys.stderr, level='INFO', format='{time:HH:mm:ss} | {level} | {message}')
    try:
        await run(args.proxy, args.output_dir, args.timeout)
    except Exception as exc:
        logger.error('{}: {}', type(exc).__name__, str(exc))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
