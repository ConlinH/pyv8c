"""Generate a fresh reCAPTCHA token in pyv8c and verify an intentionally invalid login."""

import asyncio
import argparse
from html import escape
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from threading import Lock
import time
from urllib.parse import urljoin, urlsplit

sys.dont_write_bytecode = True
WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(WORKSPACE / 'pyv8'), str(WORKSPACE / 'pyv8c')]

from pyv8c import pyv8
from loguru import logger
from pyv8c.async_api import Browser

HOME = 'https://app.checkinpax.com/'
LOGIN = 'https://api.checkinpax.com/api/login'
EXPECTED_MESSAGE = 'Invalid email or password.'
EMAIL = 'pyv8c-invalid-check@example.com'
PASSWORD = 'Invalid-CheckOnly-2026!'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36')
SITE_KEY = re.compile(r'''["'`](6L[A-Za-z0-9_-]{38})["'`]''')
SDK_HOSTS = {'www.google.com', 'www.gstatic.com', 'fonts.gstatic.com'}


async def run(proxy=None, timeout=90):
    """Everything used for verification is fetched or generated during this call."""
    started = time.monotonic()
    state = {'verified': False, 'target': HOME}
    issued_tokens, static_responses = set(), {}
    lock = Lock()
    page = browser = None

    def request(method, url, **kwargs):
        """Only safe GETs retry; a verification or login POST is sent once."""
        for attempt in range(3 if method == 'GET' else 1):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('The local verification flow exceeded its deadline.')
            try:
                response = page.profile.network_backend.request_sync(page.net.build(method, url, timeout=min(20, remaining), **kwargs))
                parts = urlsplit(url)
                logger.info('{} {}{} -> {}', method, parts.netloc, parts.path, response.status)
                return response
            except Exception as error:
                parts = urlsplit(url)
                logger.warning('{} {}{} failed ({}/{}): {}', method, parts.netloc, parts.path,
                               attempt + 1, 3 if method == 'GET' else 1, error)
                if method != 'GET' or attempt == 2:
                    raise

    def intercept(url, resource):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https'):
            return None
        if parts.hostname not in SDK_HOSTS:
            return False
        method, body = resource.get('method', 'GET'), resource.get('body')
        with lock:
            cached = static_responses.get(url)
        if cached is not None:
            return cached
        if body is not None and not isinstance(body, (bytes, str)):
            raise TypeError('A request body crossed out of V8 without being serialized.')
        # print(resource.get('headers'))
        response = request(method, url, headers=resource.get('headers'), data=body)
        if parts.path == '/recaptcha/api2/reload' and method == 'POST':
            state['reload_status'] = response.status
            state['reload_request_bytes'] = len(body) if body else 0
            envelope = response.text
            if envelope.startswith(")]}'"):
                envelope = envelope.split('\n', 1)[1]
            parsed = json.loads(envelope)
            if (response.status == 200 and isinstance(parsed, list) and len(parsed) > 1
                    and parsed[0] == 'rresp' and isinstance(parsed[1], str) and parsed[1]):
                with lock:
                    issued_tokens.add(parsed[1])
        result = {'status': response.status, 'headers': response.headers, 'body': response.content}
        if parts.hostname in {'www.gstatic.com', 'fonts.gstatic.com'} and response.status == 200:
            with lock:
                static_responses[url] = result
        return result

    try:
        browser = (await Browser(
            cache=False, proxy=proxy, resource_interceptor=intercept, stub_logging='off',
            # environment={'user_agent': USER_AGENT},
            http_options={'impersonate': 'chrome150', 'timeout': 20},
        ).__aenter__())
        page = browser.new_profile().new_page()
        home = (await asyncio.to_thread(request, 'GET', HOME))
        state['home_status'] = home.status
        if home.status != 200:
            raise RuntimeError(f'Homepage returned {home.status}.')
        keys = set(SITE_KEY.findall(home.text))
        if not keys:
            scripts = re.findall(r'''<script\b[^>]*\bsrc=["']([^"']+)["']''', home.text, re.I)
            for src in scripts:
                url = urljoin(HOME, src)
                if urlsplit(url).hostname != 'app.checkinpax.com' or '/assets/' not in url:
                    continue
                asset = (await asyncio.to_thread(request, 'GET', url))
                if asset.status != 200:
                    raise RuntimeError(f'Application script returned {asset.status}.')
                keys.update(SITE_KEY.findall(asset.text))
        if len(keys) != 1:
            raise RuntimeError('Cannot identify one current reCAPTCHA site key in the application.')
        key = keys.pop()
        # The application's execute call is reproduced with the original SDK.
        # No React build, captured token, modified SDK or external browser is used.
        (await page.set_content('''<!doctype html><html><head>
            <script src="https://www.google.com/recaptcha/api.js?render=%s"></script>
            </head><body><form id="login"><button id="submit" type="submit">Log in</button></form>
            <script>window.localToken=null;window.localErrors=[];
            document.getElementById('login').onsubmit=function(event){event.preventDefault();
                grecaptcha.ready(function(){grecaptcha.execute(%s,{action:'submit'})
                    .then(function(token){window.localToken=token;},
                          function(error){window.localErrors.push(String(error));});});};
            </script></body></html>''' % (escape(key, quote=True), json.dumps(key)), url=HOME))
        initialized = time.monotonic()
        clicked = False
        while time.monotonic() - started < timeout:
            (await asyncio.sleep(.1))
            current = page.evaluate('''({ready:typeof grecaptcha==='object'&&
                typeof grecaptcha.execute==='function',token:window.localToken,
                errors:window.localErrors})''')
            errors = current.get('errors')
            if isinstance(errors, list) and errors:
                raise RuntimeError('SDK execute failed: ' + '; '.join(errors))
            # Allow the invisible widget and its worker to initialize, as they do
            # while a user fills the login form. This does not supply any proof.
            if current.get('ready') and not clicked and time.monotonic() - initialized >= 12:
                (await page.click('#submit', force=True, delay=.06))
                clicked = True
            token = current.get('token')
            if isinstance(token, str) and token:
                print('reCaptchaToken:', token, flush=True)
                with lock:
                    issued_here = token in issued_tokens
                if not issued_here:
                    raise RuntimeError('The SDK returned a fallback token, not a token from this reload.')
                state['reCaptchaToken_length'] = len(token)
                response = (await asyncio.to_thread(request, 'POST', LOGIN, json={
                    'email': EMAIL, 'password': PASSWORD, 'reCaptchaToken': token,
                }, headers={'Origin': HOME.rstrip('/'), 'Referer': HOME}))
                print('login response:', response.text, flush=True)
                state['login_status'] = response.status
                state['response'] = json.loads(response.text)
                state['verified'] = (response.status == 200 and
                                     state['response'].get('message') == EXPECTED_MESSAGE)
                break
        else:
            raise TimeoutError('The SDK did not return a token before the deadline.')
    except (Exception, pyv8.JSException) as error:
        state['error'] = f'{type(error).__name__}: {error}'
    finally:
        state['elapsed_seconds'] = round(time.monotonic() - started, 2)
        if browser is not None:
            await browser.close()
    return state


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default='http://127.0.0.1:6696',
                        help='For example http://127.0.0.1:6696; defaults to RECAPTCHA_PROXY.')
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    logger.remove()
    logger.add(sys.stderr, level='INFO', format='{time:HH:mm:ss} | {message}')
    if args.worker:
        result = (await run(args.proxy, args.timeout))
        if result.get('error'):
            logger.error('{}', result['error'])
        logger.info('verified={} elapsed={}s', result['verified'], result['elapsed_seconds'])
        return 0 if result['verified'] else 1
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
               '--timeout', str(args.timeout)]
    if args.proxy:
        command.extend(['--proxy', args.proxy])
    child = subprocess.Popen(command, start_new_session=os.name != 'nt')
    try:
        return (await asyncio.to_thread(child.wait, timeout=args.timeout + 25))
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                           capture_output=True, timeout=10, check=False)
        else:
            os.killpg(child.pid, signal.SIGKILL)
        (await asyncio.to_thread(child.wait, timeout=10))
        logger.error('The isolated run stopped: {}', type(error).__name__)
        return 124 if isinstance(error, subprocess.TimeoutExpired) else 130


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
