"""用 pyv8c 生成当次 reCAPTCHA Token，以无效测试凭据检查登录接口。"""
import argparse
import asyncio
from html import escape
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import pyv8, register_transport
from pyv8c.async_api import Browser
from pyv8c.network.curl_cffi import CurlCffiTransport

HOME = 'https://app.checkinpax.com/'
LOGIN = 'https://api.checkinpax.com/api/login'
EXPECTED_MESSAGE = 'Invalid email or password.'
SITE_KEY = re.compile(r'''["'`](6L[A-Za-z0-9_-]{38})["'`]''')
SDK_HOSTS = {'www.google.com', 'www.gstatic.com', 'fonts.gstatic.com'}
DEFAULT_PROXY = 'http://127.0.0.1:6696'


def trim_resources(url, request):
    target = urlsplit(url)
    if target.scheme in {'http', 'https'} and target.hostname not in SDK_HOSTS:
        return False


async def find_site_key(page):
    home = await page.net.get(HOME, timeout=20)
    if home.status != 200:
        raise RuntimeError(f'首页 HTTP {home.status}')
    keys = set(SITE_KEY.findall(home.text))
    if not keys:
        for src in re.findall(r'''<script\b[^>]*\bsrc=["']([^"']+)["']''', home.text, re.I):
            url = urljoin(HOME, src)
            if urlsplit(url).hostname == 'app.checkinpax.com' and '/assets/' in url:
                response = await page.net.get(url, timeout=20)
                if response.status != 200:
                    raise RuntimeError(f'应用脚本 HTTP {response.status}')
                keys.update(SITE_KEY.findall(response.text))
    if len(keys) != 1:
        raise RuntimeError('无法从当前应用中识别唯一的 site key')
    return keys.pop()


async def run(*, proxy=DEFAULT_PROXY, timeout=90):
    started = time.monotonic()
    issued_tokens = set()

    class Transport(CurlCffiTransport):
        async def _single_call(self, request):
            response = await super()._single_call(request)
            if (urlsplit(request.url).path == '/recaptcha/api2/reload'
                    and request.method == 'POST' and response.status == 200):
                envelope = response.text
                if envelope.startswith(")]}'"):
                    envelope = envelope.split('\n', 1)[1]
                parsed = json.loads(envelope)
                if (isinstance(parsed, list) and len(parsed) > 1 and parsed[0] == 'rresp'
                        and isinstance(parsed[1], str) and parsed[1]):
                    issued_tokens.add(parsed[1])
            return response

    register_transport('recaptcha-demo', Transport, proxy=True)
    logger.info('加载当前应用与 reCAPTCHA SDK（代理：{}）', proxy or '直连')
    async with asyncio.timeout(timeout), Browser(
        proxy=proxy, cache=False, resource_interceptor=trim_resources,
        http_engine='recaptcha-demo', http_options={'impersonate': 'chrome150', 'timeout': 20},
    ) as browser:
        page = browser.new_profile().new_page()
        key = await find_site_key(page)
        logger.info('reCAPTCHA sitekey: {}', key)
        await page.set_content('''<!doctype html><script src="https://www.google.com/recaptcha/api.js?render=%s"></script>
          <form id="login"><button id="submit" type="submit">Log in</button></form>
          <script>globalThis.result={token:null,error:null};
          document.getElementById('login').onsubmit=function(event){event.preventDefault();
            grecaptcha.ready(function(){grecaptcha.execute(%s,{action:'submit'}).then(
              token=>result.token=token, error=>result.error=String(error));});};</script>
          ''' % (escape(key, quote=True), json.dumps(key)), url=HOME)
        await page.wait_for("typeof grecaptcha==='object' && typeof grecaptcha.execute==='function'",
                            timeout=timeout)
        await page.sleep(12)  # 沿用原案例的 SDK 初始化等待。
        await page.click('#submit', force=True, delay=.06)
        await page.wait_for('result.token !== null || result.error !== null', timeout=timeout)
        result = json.loads(page.evaluate('JSON.stringify(result)'))
        token = result['token']
        if not isinstance(token, str) or not token or token not in issued_tokens:
            raise RuntimeError(result['error'] or 'SDK 未返回本次 reload 签发的 Token')
        logger.info('reCaptchaToken: {}', token)
        response = await page.net.post(LOGIN, json={
            'email': 'pyv8c-invalid-check@example.com', 'password': 'Invalid-CheckOnly-2026!',
            'reCaptchaToken': token,
        }, headers={'Origin': HOME.rstrip('/'), 'Referer': HOME}, timeout=20)
        data = response.json()
        if (response.status != 200 or not isinstance(data, dict)
                or data.get('message') != EXPECTED_MESSAGE):
            raise RuntimeError(f'验收失败：HTTP {response.status}, {response.text[:200]}')
        return {'response': data, 'token': token, 'elapsed': round(time.monotonic() - started, 2)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default=DEFAULT_PROXY, help='代理地址；传 none 表示直连')
    parser.add_argument('--timeout', type=float, default=90)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout 必须为正数')
    proxy = None if args.proxy.lower() in {'none', ''} else args.proxy
    logger.remove()
    logger.add(sys.stdout, level='INFO')
    try:
        result = await run(proxy=proxy, timeout=args.timeout)
    except (Exception, pyv8.JSException, pyv8.JavaScriptTerminated) as error:
        logger.error('验收失败：{}: {}', type(error).__name__, error)
        return 1
    logger.info('登录响应：{}', json.dumps(result['response'], ensure_ascii=False))
    logger.success('验证通过，Token 长度 {}（耗时 {}s）', len(result['token']), result['elapsed'])
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
