"""用 pyv8c 原生执行页面上的 Turnstile 挑战。

不依赖 Chrome、HAR、预置 Cookie 或 cfsolve；脚本由框架下载。有可点的
Turnstile 复选框才点击，没有则只等结果。成功判据是最后一次顶层导航 POST
的状态不是 403。
"""
from __future__ import annotations

import argparse
import asyncio
import ssl
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

from pyv8c.foundation import profile_path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import register_transport
from pyv8c.async_api import Browser
from pyv8c.foundation.host_errors import HostStateError, RealmBusyError
from pyv8c.network.curl_cffi import CurlCffiTransport

# URL = 'https://pastebin.com/login'
# URL = 'https://www.e-food.gr'
# URL = "https://fractal-testnet.unisat.io/explorer"
# URL = "https://cn.airbusan.com/content/individual"
URL = "https://simple.ripley.com.pe/"
# URL = 'https://steamdb.info/login/'
URL = 'https://stackoverflow.com/questions/59187905/proper-procedure-to-restart-reload-apache-php-fpm-combo'

DEFAULT_PROXY = 'http://127.0.0.1:7890'
CHECKBOX = 'input[type="checkbox"]'


def unverified_tls():
    """代理换证时让 wss 与 HTTP 的 verify=False 一样握上手。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def navigation_post(request):
    """顶层文档导航 POST，且 scheme、host、path 与当前 URL 相同。空 path 与 `/` 都算站点根。"""
    if (str(request.method).upper() != 'POST' or request.mode != 'navigate'
            or not request.top_level_navigation):
        return False
    page = urlsplit(URL)
    sent = urlsplit(request.url)
    page_path = page.path if page.path else '/'
    sent_path = sent.path if sent.path else '/'
    return sent.scheme == page.scheme and sent.hostname == page.hostname and sent_path == page_path


def trim_resources(url, request):
    """丢弃站点自身的展示资源，保留挑战域的一切和所有脚本/网络请求。
    challenges.cloudflare.com 的 image 参与挑战，不能按类型一律丢弃。
    """
    if request.get('resource_type') in {'image', 'style', 'font', 'media'}:
        host = urlsplit(url).hostname or ''
        if not host.endswith('challenges.cloudflare.com'):
            return False


async def run(*, proxy=DEFAULT_PROXY, timeout=120):
    started = time.monotonic()
    deadline = started + timeout
    logger.info('开始挑战，打开 {}（代理：{}）', URL, proxy or '直连')
    posts = []
    posts_lock = threading.Lock()

    class ObservedTransport(CurlCffiTransport):
        async def _send(self, request):
            response = await super()._send(request)
            if navigation_post(request):
                record = {
                    'status': int(response.status),
                    'method': 'POST',
                    'url': str(response.url),
                }
                with posts_lock:
                    posts.append(record)
                logger.info('导航 POST {} HTTP {}', record['url'], record['status'])
            return response

    engine = register_transport('5s-observed', ObservedTransport, proxy=True)
    async with Browser(
            environment=profile_path("darwin"),
            cache=False, threaded_loaders=True, proxy=proxy, gpu_enabled=False,
            resource_interceptor=trim_resources,
            http_engine=engine,
            http_options={'impersonate': 'chrome150', 'timeout': 20, 'retry': 5},
            ws_options={'ssl': unverified_tls()},
    ) as browser:
        page = browser.new_profile().new_page()
        await page.goto(URL, wait_until='domcontentloaded', timeout=min(30, timeout))
        title = page.evaluate('document.title')
        logger.info(
            '页面已加载，title={!r}，等待导航 POST；出现可点的 Turnstile 复选框才点击',
            title,
        )
        clicked = set()

        def ready_turnstile_checkbox():
            # 在所属 realm 线程上走 deep 根：光树、closed shadow、再按 DOM 顺序进子 frame。
            # 只认 challenges.cloudflare.com 且路径含 /turnstile/f/ 的 frame，站点自己的框不点。
            from pyv8c.web.css.selectors import select
            from pyv8c.web.dom.hit_testing import border_box

            for owner, root in page._deep_roots():
                try:
                    target = urlsplit(owner.document.url)
                except HostStateError:
                    continue
                if (target.hostname != 'challenges.cloudflare.com'
                        or '/turnstile/f/' not in target.path):
                    continue
                for node in select(root, CHECKBOX):
                    if id(node) in clicked:
                        continue
                    try:
                        box = border_box(node)
                    except HostStateError:
                        continue
                    if box.width <= 0 or box.height <= 0:
                        continue
                    return node, owner
            return None

        # title 只记变化。通过看最后一次顶层导航 POST：不是 403 才返回。
        # 403 留在循环里，直到超时。框没画出来或被挡住时不记已点。
        last_title = title
        last_post = None
        while time.monotonic() < deadline:
            try:
                current = page.evaluate('document.title')
                if current != last_title:
                    logger.info('页面 title 变为 {!r}', current)
                    last_title = current
            except Exception:
                pass
            try:
                found = page.realm.executor.call(ready_turnstile_checkbox)
            except (RealmBusyError, HostStateError):
                found = None
            if found is not None:
                node, owner = found
                try:
                    await owner.mouse.click_element(node, delay=.08)
                except (HostStateError, RealmBusyError):
                    pass
                else:
                    clicked.add(id(node))
                    logger.info('已点击验证复选框，第 {} 次', len(clicked))
            with posts_lock:
                if posts:
                    last_post = posts[-1]
            if last_post is not None and last_post['status'] != 403:
                logger.info(
                    '最后一次导航 POST HTTP {}，可使用 page.net 获取目标数据',
                    last_post['status'],
                )
                return {
                    'elapsed': round(time.monotonic() - started, 2),
                    'status': last_post['status'],
                    'method': last_post['method'],
                    'url': last_post['url'],
                }
            await page.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        if last_post is None:
            raise TimeoutError(f'{timeout:g}s 内未观察到目标页面的导航 POST')
        raise TimeoutError(
            f'{timeout:g}s 内最后一次导航 POST 仍是 HTTP {last_post["status"]}'
        )


async def main():
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
        result = await run(proxy=proxy, timeout=args.timeout)
    except Exception as error:
        logger.error('挑战失败：{}: {}', type(error).__name__, error)
        return 1
    logger.success(
        '挑战通过：{} {} → HTTP {}（耗时 {}s）',
        result['method'], result['url'], result['status'], result['elapsed'],
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
