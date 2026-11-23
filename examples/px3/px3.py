"""用 pyv8c 打开 PX 挑战页，按住按钮并等待离开 captcha 页面。"""
import argparse
import asyncio
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import pyv8
from pyv8c.async_api import Browser

DEFAULT_URL = 'https://www.skyscanner.com.au/sttc/px/captcha-v2/index.html?url=Lz8=&uuid=b74291f0-c061-11f1-ae58-2588bb3de0f3&vid=72d0bd15-c061-11f1-9900-dad3975d6'
DEFAULT_PROXY = 'http://127.0.0.1:6696'
READY = '''(() => {
  const node = document.querySelector('#px-captcha');
  return !!node && node.getBoundingClientRect().height > 4 && /press/i.test(node.textContent || '');
})()'''
BUTTON = '''(() => {
  const node = document.querySelector('#px-captcha');
  let box = null;
  for (const element of node.querySelectorAll('*')) {
    const rect = element.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8 || !/press/i.test(element.textContent || '')) continue;
    if (!box || rect.width < box.width) box = rect;
  }
  box ||= node.getBoundingClientRect();
  return {x:box.x + box.width / 2, y:box.y + box.height / 2};
})()'''


def log_payload(url, request):
    if request.get('method', 'GET').upper() == 'POST' and '/xhr/' in urlsplit(url).path:
        logger.info('PX3 payload: {}', request.get('body'))


async def run(*, url=DEFAULT_URL, proxy=DEFAULT_PROXY, timeout=120, hold=2.5):
    started = time.monotonic()
    logger.info('打开 {}（代理：{}）', url, proxy or '直连')
    async with asyncio.timeout(timeout), Browser(
        proxy=proxy, cache=False, threaded_loaders=True,
        resource_interceptor=log_payload,
        http_options={'impersonate': 'chrome150', 'timeout': 20},
    ) as browser:
        page = browser.new_profile().new_page()
        await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        await page.wait_for(READY, timeout=timeout)
        spot = page.evaluate(BUTTON)
        await page.mouse.move(spot['x'] - 150, spot['y'] + 60, steps=15)
        await page.mouse.move(spot['x'], spot['y'], steps=10)
        logger.info('按钮已就绪，按住 {}s', hold)
        await page.mouse.down()
        try:
            deadline = time.monotonic() + hold
            direction = 1
            while time.monotonic() < deadline:
                await page.mouse.move(spot['x'] + direction, spot['y'], steps=1)
                direction *= -1
                await page.sleep(min(.25, max(0, deadline - time.monotonic())))
        finally:
            await page.mouse.up()
        await page.wait_for("!location.pathname.startsWith('/sttc/px/captcha')", timeout=timeout)
        logger.info('Cookie: {}', page.profile.cookies.header_for(page.evaluate('location.href')))
        return {'url': page.evaluate('location.href'), 'title': page.evaluate('document.title'),
                'elapsed': round(time.monotonic() - started, 2)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL, help='当前挑战页 URL，含当次 uuid 和 vid')
    parser.add_argument('--proxy', default=DEFAULT_PROXY, help='代理地址；传 none 表示直连')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--hold', type=float, default=2.5)
    args = parser.parse_args()
    if args.timeout <= 0 or args.hold <= 0:
        parser.error('--timeout 和 --hold 必须为正数')
    proxy = None if args.proxy.lower() in {'none', ''} else args.proxy
    logger.remove()
    logger.add(sys.stdout, level='INFO')
    try:
        result = await run(url=args.url, proxy=proxy, timeout=args.timeout, hold=args.hold)
    except (Exception, pyv8.JSException, pyv8.JavaScriptTerminated) as error:
        logger.error('运行失败：{}: {}', type(error).__name__, error)
        return 1
    logger.success('已离开 captcha 页面：{}；标题 {!r}（耗时 {}s）',
                   result['url'], result['title'], result['elapsed'])
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
