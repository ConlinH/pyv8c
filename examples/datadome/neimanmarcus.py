"""用 pyv8c 生成 payload、plv3，提交一次并打印响应中的 datadome 值。"""
import argparse
import asyncio
from pathlib import Path
import re
import sys
from threading import Event
import time
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import NetworkResponse, pyv8, register_transport
from pyv8c.async_api import Browser
from pyv8c.network.curl_cffi import CurlCffiTransport

URL = 'https://www.neimanmarcus.com/'
DEFAULT_PROXY = 'http://127.0.0.1:6696'
CHALLENGE_HOSTS = {'ct.captcha-delivery.com', 'geo.captcha-delivery.com',
                   'static.captcha-delivery.com'}
REQUIRED = ('payload', 'plv3')


def datadome_value(response):
    if response.status != 200:
        raise RuntimeError(f'Interstitial HTTP {response.status}: {response.text[:200]}')
    try:
        data = response.json()
    except ValueError as error:
        content_type = next((value for name, value in response.headers.items()
                             if name.lower() == 'content-type'), '(missing)')
        raise RuntimeError(f'Interstitial 返回非 JSON：HTTP 200，Content-Type={content_type}') from error
    if not isinstance(data, dict):
        raise RuntimeError('Interstitial 响应不是 JSON 对象：HTTP 200')
    assignment = data.get('cookie')
    if not isinstance(assignment, str):
        raise RuntimeError('响应缺少 DataDome Cookie')
    name, separator, value = assignment.split(';', 1)[0].partition('=')
    if name.strip() != 'datadome' or not separator or not value.strip():
        raise RuntimeError('响应缺少非空 DataDome Cookie')
    return value.strip()


async def run(*, proxy=DEFAULT_PROXY, timeout=60):
    started = time.monotonic()
    ready = Event()
    captured = {}

    def intercept(url, request):
        target = urlsplit(url)
        if target.scheme not in {'http', 'https'}:
            return None
        if ready.is_set() or not (target.hostname in CHALLENGE_HOSTS or url == URL):
            return False
        method = request.get('method', 'GET').upper()
        if method == 'POST' and target.hostname == 'geo.captcha-delivery.com' and target.path == '/interstitial/':
            body = request.get('body') or ''
            if isinstance(body, bytes):
                body = body.decode('utf-8')
            captured.update(url=url, body=body, headers=dict(request.get('headers') or {}))
            ready.set()
            # 保留原案例的提交边界：拦下这次请求，再由宿主原样发送一次。
            return {'status': 400, 'body': '{"error":"request captured"}',
                    'headers': {'content-type': 'application/json'}}
        if target.path.startswith('/captcha/'):
            captured['error'] = '站点改为交互验证码，本例仅处理 interstitial'
            ready.set()
            return False

    class Transport(CurlCffiTransport):
        async def _single_call(self, request):
            response = await super()._single_call(request)
            if request.url == URL and not ready.is_set():
                if response.status == 200:
                    captured['error'] = '首页未下发 interstitial，无法生成当次 payload 和 plv3'
                    ready.set()
                    return NetworkResponse(url=URL, body='<!doctype html><title>Initial response captured</title>',
                                           headers={'content-type': 'text/html'})
                if response.status != 403 or not re.search(r'\bvar\s+dd\s*=', response.text):
                    captured['error'] = f'未收到 dd interstitial，HTTP {response.status}'
                    ready.set()
            return response

    register_transport('datadome-demo', Transport, proxy=True)
    async with asyncio.timeout(timeout), Browser(
        proxy=proxy, cache=False, resource_interceptor=intercept,
        http_engine='datadome-demo',
        http_options={'impersonate': 'chrome150', 'timeout': 20},
    ) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        await page.goto(URL, wait_until='domcontentloaded', timeout=timeout)
        while not ready.is_set():
            await page.sleep(.1)
        if captured.get('error'):
            raise RuntimeError(captured['error'])
        params = parse_qs(captured['body'], keep_blank_values=True)
        missing = [name for name in REQUIRED if len(params.get(name, [])) != 1 or not params[name][0]]
        if missing:
            raise RuntimeError('缺少 SDK 参数：' + ', '.join(missing))
        payload, plv3 = params['payload'][0], params['plv3'][0]
        logger.info('payload: {}', payload)
        logger.info('plv3: {}', plv3)
        response = await page.net.post(captured['url'], data=captured['body'],
                                       headers=captured['headers'], timeout=20)
        datadome = datadome_value(response)
        logger.info('datadome: {}', datadome)
        return {'payload': payload, 'plv3': plv3, 'datadome': datadome,
                'elapsed': round(time.monotonic() - started, 2)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default=DEFAULT_PROXY, help='代理地址；传 none 表示直连')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout 必须为正数')
    proxy = None if args.proxy.lower() in {'none', ''} else args.proxy
    logger.remove()
    logger.add(sys.stdout, level='INFO', filter=lambda record: record['name'] == __name__)
    try:
        await run(proxy=proxy, timeout=args.timeout)
    except (Exception, pyv8.JSException, pyv8.JavaScriptTerminated) as error:
        logger.error('运行失败：{}: {}', type(error).__name__, error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
