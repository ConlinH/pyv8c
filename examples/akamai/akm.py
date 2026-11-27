
import asyncio
import time
import sys

from loguru import logger
from pyv8c.async_api import Browser

logger.remove()
logger.add(sys.stdout, level='INFO')

# URL = "https://www.bestbuy.com"
# URL = "https://www.koreanair.com"
URL = "https://www.radissonhotels.com/zh-cn/"

# PROXY = "http://127.0.0.1:7890"
PROXY = None


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
        # trace=True, traceable=True,
        resource_interceptor=intercept,
        proxy=PROXY,
        http_engine='curl_cffi', http_options={"impersonate": "chrome150"},
        threaded_loaders=True,
    ) as browser:
        profile = browser.new_profile()
        page = profile.new_page()

        await page.goto(URL, wait_until="domcontentloaded", timeout=60)

        # 传感器看的是光标：逐帧靠近，落到终点后按住一会儿再松开（人手一次点击几十毫秒）。
        for x, y, seed in ((360, 220, 1), (700, 380, 2), (940, 470, 3)):
            await page.mouse.move(x, y, curve="human", steps=8, duration=8 / 60, seed=seed)
            await page.run_until_idle(timeout=0)
        await page.mouse.click(delay=0.07)
        await page.run_until_idle(timeout=0)

        cookies = await wait_for_cookie(profile, page, URL)

    logger.info(f"cookies: {cookies}")
    logger.info('已成功加载cookies，可使用page.net.get/post/xhr/fetch等接口直接获取目标数据')
    logger.info(f"use time: {time.time() - st}")


if __name__ == "__main__":
    asyncio.run(main())
