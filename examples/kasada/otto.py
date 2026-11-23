"""用 pyv8c 打开 OTTO 搜索页，提取并打印前 10 个商品。"""
import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import pyv8, register_transport
from pyv8c.async_api import Browser
from pyv8c.network.curl_cffi import CurlCffiTransport

DEFAULT_URL = 'https://www.otto.de/suche/banana/'
DEFAULT_PROXY = 'http://127.0.0.1:6696'
CHALLENGE_FILES = {'ips.js', 'p.js', 'rc.js', 'fp', 'mfc', 'tl'}
PRODUCT = 'article[data-qa="reptile-product-tile"]'


def extract_products(page):
    # 直接读取当前页面 DOM；占位卡片没有价格时保留 None。
    cards = json.loads(page.evaluate(r'''JSON.stringify(Array.from(document.querySelectorAll('article[data-qa="reptile-product-tile"]'), card => {
      const text = selector => (card.querySelector(selector)?.textContent || '').replace(/\s+/g, ' ').trim();
      const attr = name => card.getAttribute(name);
      const decodeLink = value => new TextDecoder().decode(Uint8Array.from(atob(value), c => c.charCodeAt(0)));
      const link = card.querySelector('[data-qa="find_tile__productLink"], a.reptile-tile-placeholder__link');
      const image = card.querySelector('img.reptile-product-image__image');
      const price = card.querySelector('ofc-pricing-item-v1');
      const href = link?.getAttribute('href') || (link?.getAttribute('base64-href') ? decodeLink(link.getAttribute('base64-href')) : '');
      const brand = text('.reptile-product-brand');
      const title = text('.reptile-product-title') || text('a.reptile-tile-placeholder__link');
      return {product_id:attr('data-product-id'), variation_id:attr('data-variation-id'),
        position:attr('data-list-position'), sponsored:attr('data-origin') === 'sponsored',
        brand, title, name:title ? [brand,title].filter(Boolean).join(' ') : (image?.getAttribute('alt') || ''),
        url:href ? new URL(href, location.href).href : null,
        image:image?.getAttribute('src') ? new URL(image.getAttribute('src'), location.href).href : null,
        price:price?.getAttribute('retail-price') ?? null,
        original_price:price?.getAttribute('suggested-retail-price') ?? null,
        currency:price ? 'EUR' : null, starting_price:price?.getAttribute('is-starting-price') === 'true'};
    }))'''))
    products, seen = [], set()
    for product in cards:
        if not all(product.get(key) for key in ('product_id', 'name', 'url')):
            raise RuntimeError('商品卡片缺少 ID、名称或链接')
        for key in ('price', 'original_price'):
            if product[key] is not None:
                product[key] = format(Decimal(product[key]) / 100, '.2f')
        if product['position'] and product['position'].isdigit():
            product['position'] = int(product['position'])
        key = product['variation_id'] or product['product_id']
        if key not in seen:
            seen.add(key)
            products.append(product)
    if not products:
        raise RuntimeError('搜索页未提取到商品')
    return products


async def run(*, url=DEFAULT_URL, proxy=DEFAULT_PROXY, timeout=65):
    target = urlsplit(url)
    if target.scheme != 'https' or target.hostname != 'www.otto.de':
        raise ValueError('目标必须是 www.otto.de 的 HTTPS 地址')
    started = time.monotonic()
    state = {}

    def trim_resources(link, request):
        parts = urlsplit(link)
        if parts.scheme in {'http', 'https'} and not (
            parts.hostname == target.hostname and
            (parts.path == target.path or parts.path.rsplit('/', 1)[-1] in CHALLENGE_FILES)
        ):
            return False

    class Transport(CurlCffiTransport):
        async def _single_call(self, request):
            parts = urlsplit(request.url)
            if parts.hostname == target.hostname and parts.path.endswith('/tl') and request.method == 'POST':
                logger.info('Kasada headers: {}', json.dumps(
                    {key: value for key, value in request.headers.items()
                               if key.lower().startswith('x-kpsdk-')}, ensure_ascii=False))
            response = await super()._single_call(request)
            if parts.hostname == target.hostname:
                if parts.path == target.path and request.method == 'GET' and response.status == 200:
                    state['response'] = response
                elif parts.path.endswith('/tl') and request.method == 'POST':
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
                    if response.status != 200 or not isinstance(data, dict) or data.get('reload') is not True:
                        state['error'] = f'/tl 拒绝：HTTP {response.status}, {response.text[:200]}'
                    else:
                        logger.info('/tl HTTP 200，等待搜索页重载')
            return response

    register_transport('otto-demo', Transport, proxy=True)
    logger.info('打开 {}（代理：{}）', url, proxy or '直连')
    async with asyncio.timeout(timeout), Browser(
        proxy=proxy, cache=False, threaded_loaders=True, resource_interceptor=trim_resources,
        http_engine='otto-demo', http_options={'impersonate': 'chrome150', 'timeout': 25},
    ) as browser:
        page = browser.new_profile().new_page()
        await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        while 'response' not in state:
            if state.get('error'):
                raise RuntimeError(state['error'])
            await page.sleep(.1)
        await page.wait_for_selector(PRODUCT, timeout=timeout)
        products = extract_products(page)
        return {'products': products, 'html_bytes': len(state['response'].content),
                'elapsed': round(time.monotonic() - started, 2)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--proxy', default=DEFAULT_PROXY, help='代理地址；传 none 表示直连')
    parser.add_argument('--timeout', type=float, default=65)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout 必须为正数')
    proxy = None if args.proxy.lower() in {'none', ''} else args.proxy
    logger.remove()
    logger.add(sys.stdout, level='INFO')
    try:
        result = await run(url=args.url, proxy=proxy, timeout=args.timeout)
    except (Exception, pyv8.JSException, pyv8.JavaScriptTerminated) as error:
        logger.error('查询失败：{}: {}', type(error).__name__, error)
        return 1
    logger.success('搜索页 HTTP 200，共 {} 个商品（耗时 {}s）', len(result['products']), result['elapsed'])
    for index, product in enumerate(result['products'][:10], 1):
        price = f"{product['price']} EUR" if product['price'] is not None else '未提供价格'
        logger.info('{:02d}. {}{} | {}{} | {}', index, product['name'],
                    ' [广告]' if product['sponsored'] else '', price,
                    ' 起' if product['starting_price'] else '', product['url'])
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
