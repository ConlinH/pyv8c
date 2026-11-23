"""用 pyv8c 执行当次 hCaptcha SDK，取得 P1_ Token。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import pyv8
from pyv8c.async_api import Browser

URL = 'https://dashboard.hcaptcha.com/login'
SITEKEY = 'e4b28873-6852-49c0-9784-7231f004b96b'
DEFAULT_PROXY = 'http://127.0.0.1:6696'


def trim_resources(url, request):
    target = urlsplit(url)
    host, path = target.hostname or '', target.path
    if target.scheme not in {'http', 'https'}:
        return None
    allowed = (
        (host == 'js.hcaptcha.com' and path == '/1/api.js')
        or (host == 'newassets.hcaptcha.com' and path.endswith(('/hcaptcha.html', '/hsw.js')))
        or (host in {'api.hcaptcha.com', 'api2.hcaptcha.com'} and
            (path == '/checksiteconfig' or path.startswith('/getcaptcha/')))
        or (host.endswith('.w.hcaptcha.com') and path == '/logo.png')
    )
    return None if allowed else False


async def run(*, proxy=DEFAULT_PROXY, timeout=60):
    started = time.monotonic()
    logger.info('加载 hCaptcha SDK（代理：{}）', proxy or '直连')
    logger.info('hCaptcha sitekey: {}', SITEKEY)
    async with asyncio.timeout(timeout), Browser(
        proxy=proxy, cache=False, threaded_loaders=True, resource_interceptor=trim_resources,
        http_options={'impersonate': 'chrome150', 'timeout': 20},
    ) as browser:
        page = browser.new_profile().new_page()
        await page.set_content('''<!doctype html><div id="captcha"></div><script>
          globalThis.result = {token:null, error:null};
          addEventListener('error', e => result.error = String(e.message || 'Script load failed'));
          addEventListener('unhandledrejection', e => result.error = String(e.reason));
          function ready() {
            const id = hcaptcha.render('captcha', {
              sitekey:SITEKEY, size:'invisible',
              'error-callback': e => result.error = String(e)
            });
            hcaptcha.execute(id, {async:true}).then(
              value => result.token = value.response,
              error => result.error = String(error));
          }
          </script><script src="https://js.hcaptcha.com/1/api.js?onload=ready&amp;render=explicit&amp;uj=true"></script>
          '''.replace('sitekey:SITEKEY', 'sitekey:' + json.dumps(SITEKEY)), url=URL)
        await page.wait_for('result.token !== null || result.error !== null', timeout=timeout)
        result = json.loads(page.evaluate('JSON.stringify(result)'))
        token = result['token']
        if not isinstance(token, str) or not token.startswith('P1_'):
            raise RuntimeError(result['error'] or '本次未取得 P1_ Token')
        logger.info('hCaptcha Token: {}', token)
        return {'token': token, 'elapsed': round(time.monotonic() - started, 2)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default=DEFAULT_PROXY, help='代理地址；传 none 表示直连')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout 必须为正数')
    proxy = None if args.proxy.lower() in {'none', ''} else args.proxy
    logger.remove()
    logger.add(sys.stdout, level='INFO')
    try:
        result = await run(proxy=proxy, timeout=args.timeout)
    except (Exception, pyv8.JSException, pyv8.JavaScriptTerminated) as error:
        logger.error('运行失败：{}: {}', type(error).__name__, error)
        return 1
    logger.success('Token 前缀 P1_，长度 {}（耗时 {}s）', len(result['token']), result['elapsed'])
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
