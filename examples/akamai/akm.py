"""用 pyv8c 打开 Korean Air，执行鼠标操作并等待有效的 _abck Cookie。"""
import argparse
import asyncio
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import pyv8
from pyv8c.async_api import Browser

URL = 'https://www.koreanair.com'
DEFAULT_PROXY = 'http://127.0.0.1:6696'


def trim_resources(url, request):
    # 沿用原案例的资源筛选。
    if request.get('resource_type') in {'image', 'style'} or '/akam/' in url or '.js' in url:
        return False


async def run(*, proxy=DEFAULT_PROXY, timeout=60):
    started = time.monotonic()
    logger.info('打开 {}（代理：{}）', URL, proxy or '直连')
    async with asyncio.timeout(timeout), Browser(
        proxy=proxy, cache=False, threaded_loaders=True,
        resource_interceptor=trim_resources,
        http_options={'impersonate': 'chrome150', 'timeout': 20},
    ) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        await page.goto(URL, wait_until='domcontentloaded', timeout=timeout)
        for x, y, seed in ((360, 220, 1), (700, 380, 2), (940, 470, 3)):
            await page.mouse.move(x, y, curve='human', steps=8, duration=8 / 60, seed=seed)
        await page.mouse.click(delay=.07)
        while True:
            cookies = profile.cookies.header_for(URL)
            if any(part.startswith('_abck=') and '~0~' in part for part in cookies.split('; ')):
                logger.info('Cookie: {}', cookies)
                return {'cookies': cookies, 'elapsed': round(time.monotonic() - started, 2)}
            await page.sleep(.25)


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
    logger.success('_abck 已就绪（耗时 {}s）', result['elapsed'])
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
