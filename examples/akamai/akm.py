
import asyncio
import time
import sys

from loguru import logger
from pyv8c.async_api import Browser

logger.remove()
logger.add(sys.stdout, level='INFO')

# PROXY = "http://127.0.0.1:7890"
PROXY = None
FRAME = 1 / 60


def intercept(url, request):
    if request.get("resource_type") in {"image", "style"}:
        return False
    if '/akam/' in url:
        return False
    if ".js" in url:
        return False
    logger.info(f"{request.get('resource_type')}, {request.get('method')} {url}")


async def wait_for_cookie(browser_ctx, page, url, seconds=30):
    waited = 0.0
    while waited < seconds:
        c = browser_ctx.cookies.header_for(url)
        for part in c.split('; '):
            if "_abck=" in part and "~0~" in part:
                return c
        await page.sleep(0.5)
        waited += 0.5


async def main():
    st = time.time()

    async with Browser(
        # stub_logging="verbose",
        # trace=True, traceable=True,
        resource_interceptor=intercept,
        proxy=PROXY,
        http_engine='curl_cffi', http_options={"impersonate": "chrome150"},
        threaded_loaders=True,
    ) as browser:
        profile = browser.new_profile()
        page = profile.new_page()

        # url = "https://www.bestbuy.com"
        url = "https://www.koreanair.com"
        # url = "https://www.radissonhotels.com/zh-cn/"
        await page.goto(url, wait_until="domcontentloaded", timeout=60)

        # 传感器看的是光标：逐帧靠近，落到终点后按住一会儿再松开（人手一次点击几十毫秒）。
        for x, y, seed in ((360, 220, 1), (700, 380, 2), (940, 470, 3)):
            await page.mouse.move(x, y, curve="human", steps=8, duration=8 / 60, seed=seed)
            await page.run_until_idle(timeout=0)
        await page.mouse.click(delay=0.07)
        await page.run_until_idle(timeout=0)

        cookies = await wait_for_cookie(profile, page, url)


    logger.info(f"cookies: {cookies}")
    logger.info(f"use time: {time.time() - st}")


if __name__ == "__main__":
    asyncio.run(main())
