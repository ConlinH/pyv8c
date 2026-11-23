"""用 pyv8c 原生执行 SteamDB Turnstile 挑战，并以登录页 title 验收。

不依赖 Chrome、HAR、预置 Cookie 或 cfsolve；脚本由框架下载，复选框由 deep
接口穿透 closed shadow 与子 frame 定位并点击。成功判据是页面 title 从挑战页
的「请稍候…」变为登录页的「Sign In · SteamDB」。本例使用同步 API。
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import pyv8
from pyv8c.sync_api import Browser


URL = 'https://steamdb.info/login/'
DEFAULT_PROXY = 'http://127.0.0.1:6696'
CHECKBOX = 'input[type="checkbox"]'


def trim_resources(url, request):
    """丢弃站点自身的展示资源，保留挑战域的一切和所有脚本/网络请求。
    challenges.cloudflare.com 的 image 参与挑战，不能按类型一律丢弃。
    """
    if request.get('resource_type') in {'image', 'style', 'font', 'media'}:
        host = urlsplit(url).hostname or ''
        if not host.endswith('challenges.cloudflare.com'):
            return False


def run(*, proxy=DEFAULT_PROXY, timeout=120):
    started = time.monotonic()
    deadline = started + timeout
    logger.info('开始挑战，打开 {}（代理：{}）', URL, proxy or '直连')
    with Browser(
        cache=False, threaded_loaders=True, proxy=proxy, gpu_enabled=False,
        resource_interceptor=trim_resources,
        http_options={'impersonate': 'chrome150', 'timeout': 20, 'retry': 5},
    ) as browser:
        page = browser.new_profile().new_page()
        page.goto(URL, wait_until='domcontentloaded', timeout=min(30, timeout))
        title = page.evaluate('document.title')
        logger.info('页面已加载，title={!r}，等待 Turnstile 复选框', title)

        # deep 接口从 page 根穿透 closed shadow 与子 frame，找到并点击复选框。
        page.wait_for_selector(CHECKBOX, deep=True, timeout=min(60, timeout))
        logger.info('复选框已就绪，执行点击')
        page.click(CHECKBOX, deep=True, delay=.08)
        logger.info('已点击复选框，等待登录页导航')

        # 点击触发 POST 导航到登录页。page.sleep 在等待期间驱动页面任务，
        # 不执行 JS、不会抛 JavaScriptTerminated；导航瞬间 evaluate 仍可能
        # 抛该异常（继承自 BaseException），需显式捕获后下一轮重试。
        last_title = title
        while time.monotonic() < deadline:
            page.sleep(min(0.25, max(0, deadline - time.monotonic())))
            try:
                current = page.evaluate('document.title')
                if current != last_title:
                    logger.info('页面 title 变为 {!r}', current)
                    last_title = current
                    logger.info('Cookie: {}', page.profile.cookies.header_for(URL))
                    logger.info('已成功加载目标页面，可使用page.net.get/post/xhr/fetch等接口直接获取目标数据')
                    return {'elapsed': round(time.monotonic() - started, 2)}
            except pyv8.JavaScriptTerminated:
                pass  # 导航终止旧 realm 的 JS，下一轮重试
            except Exception:
                pass  # realm 替换等瞬时错误
        raise TimeoutError(f'{timeout:g}s 内未出现登录页 title {last_title!r}')


def main():
    parser = argparse.ArgumentParser(description='在 pyv8c 中执行 SteamDB 挑战并验收。')
    parser.add_argument('--proxy', default=DEFAULT_PROXY, help='代理地址；传 none 表示直连')
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout 必须为正数')
    proxy = None if args.proxy.lower() in {'none', ''} else args.proxy

    logger.remove()
    logger.add(sys.stdout, level='INFO')
    try:
        result = run(proxy=proxy, timeout=args.timeout)
    except Exception as error:
        logger.error('挑战失败：{}: {}', type(error).__name__, error)
        return 1
    logger.success('挑战通过：登录页已就绪（耗时 {}s）', result['elapsed'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
