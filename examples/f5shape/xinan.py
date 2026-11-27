"""Southwest 单程航班查询 demo；页面 fetch 触发 F5 Shape 并取 shopping 结果。

业务请求走 page.net.fetch（页面 JS 的 fetch），Shape SDK 才会注入
Ee30zvqlwf-* 请求头；page.net.get 是宿主直连，拿不到这些头。
直接运行本文件即可，日期/机场/代理/超时见 --help。
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import json
import re
import sys
from urllib.parse import urlencode, urljoin, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import register_transport
from pyv8c.async_api import Browser
from pyv8c.network.curl_cffi import CurlCffiTransport

BASE = 'https://www.southwest.com'
API = BASE + '/api/air-booking/v1/air-booking/page/air/booking/shopping'
SHAPE_HEADERS = tuple(f'Ee30zvqlwf-{s}' for s in 'ABCDFZ')
DEFAULT_DATE = (date.today() + timedelta(days=1)).isoformat()  # 明天


def show_flights(flights):
    """打印航班号、路线与成人票价。"""
    for flight in flights:
        segments = flight.get('segments', [])
        numbers = '/'.join(
            f'{s["marketingCarrierCode"]}{s["flightNumber"]}' for s in segments)
        route = ' → '.join([flight['originationAirportCode']]
                           + [s['destinationAirportCode'] for s in segments])
        fares = ', '.join(
            p.get('fare', {}).get('totalFare', {}).get('value', '--')
            for p in flight.get('fareProducts', {}).get('ADULT', {}).values())
        logger.info('{} {} → {} | {}', numbers, flight['departureDateTime'], route, fares)


async def run(*, date=DEFAULT_DATE, origin='ISP', destination='BUR', proxy=None, timeout=60):
    """查询日期 YYYY-MM-DD、起点/终点 IATA；打印并返回航班列表。"""
    origin, destination = origin.strip().upper(), destination.strip().upper()
    body = {
        'lapInfantPassengersCount': '0', 'olderChildCount': '0', 'teensCount': '0',
        'youngerChildCount': '0', 'adultsCount': '1', 'adultPassengersCount': '1',
        'destinationAirportCode': destination, 'departureDate': date,
        'departureTimeOfDay': 'ALL_DAY', 'fareType': 'USD', 'int': 'HOMEQBOMAIR',
        'originationAirportCode': origin, 'passengerType': 'ADULT', 'promoCode': '',
        'returnDate': '', 'returnTimeOfDay': 'ALL_DAY', 'tripType': 'oneway',
    }
    page_url = BASE + '/air/booking/select-depart?' + urlencode(
        {**body, 'from': origin, 'to': destination})
    config = {}

    def intercept(url, request):
        target = urlsplit(url)
        if target.scheme not in ('http', 'https'):
            return None
        keep = target.hostname == 'www.southwest.com' and (
            url in (page_url, API, config.get('bootstrap'))
            or target.path.startswith('/resources/')
            or target.path == '/assets/app/scripts/swa-common.js'
        )
        return None if keep else False

    class Transport(CurlCffiTransport):
        async def _single_call(self, request):
            if request.url == API:
                headers = {k.lower(): v for k, v in request.headers.items()}
                missing = [name for name in SHAPE_HEADERS if not headers.get(name.lower())]
                if missing:
                    raise RuntimeError(f'缺少 Shape 请求头：{", ".join(missing)}')
                for name in SHAPE_HEADERS:
                    logger.info('Shape {}: {}', name, headers[name.lower()])
            response = await super()._single_call(request)
            if request.url == page_url:
                bootstrap = re.search(
                    r'''src=["']([^"']*/swa-ui/bootstrap/[^"']+/data\.js)["']''', response.text)
                version = re.search(r'/v2/air/booking/static/([^/]+)/js/', response.text)
                if not bootstrap or not version:
                    raise RuntimeError('页面缺少 bootstrap 或应用版本')
                config['bootstrap'] = urljoin(BASE, bootstrap[1])
                config['version'] = version[1]
            elif request.url == config.get('bootstrap'):
                match = re.search(
                    r'''["'][^"']+/api-keys["']:\[function\(require,module,exports\)\{\s*module.exports\s*=\s*(\{.*?\});''',
                    response.text)
                if not match:
                    raise RuntimeError('bootstrap 缺少 API key 配置')
                config['api_key'] = json.loads(match[1])['prod']
            return response

    register_transport('southwest-demo', Transport, proxy=True)
    logger.info('查询 {} {} → {}（代理：{}）', date, origin, destination, proxy or '直连')
    async with asyncio.timeout(timeout), Browser(
        proxy=proxy, http_engine='southwest-demo',
        http_options={'impersonate': 'chrome150', 'timeout': 25},
        cache=False, resource_interceptor=intercept,
        threaded_loaders=True, stub_logging='off',
    ) as browser:
        page = browser.new_profile().new_page()
        await page.goto(page_url, wait_until='domcontentloaded', timeout=timeout)
        if not config.get('api_key'):
            raise RuntimeError('bootstrap 未加载成功，无法读取 API key')
        response = await page.net.fetch(
            API, method='POST',
            headers={
                'Accept': 'application/json, text/plain, */*', 'content-type': 'application/json',
                'x-api-key': config['api_key'], 'x-channel-id': 'southwest',
                'x-app-id': 'air-booking-v2', 'x-app-version': config['version'],
                'x-diagnostic': json.dumps({'spa': config['version']}, separators=(',', ':')),
                'x-user-experience-id': str(uuid.uuid4()),
            },
            body=json.dumps(body, separators=(',', ':')), timeout=timeout,
        )
        if response.status != 200:
            raise RuntimeError(f'Shopping HTTP {response.status}: {response.text[:1000]}')
        products = response.json().get('data', {}).get('searchResults', {}).get('airProducts')
        if not isinstance(products, list):
            raise RuntimeError(f'接口未返回航班数据：{response.text[:1000]}')
        return [f for product in products for f in product.get('details', [])]


async def main():
    parser = argparse.ArgumentParser(description='在 pyv8c 中查询 Southwest 单程航班。')
    parser.add_argument('--date', default=DEFAULT_DATE, help='出发日期 YYYY-MM-DD；默认明天')
    parser.add_argument('--origin', default='ISP')
    parser.add_argument('--destination', default='BUR')
    parser.add_argument('--proxy', default=None, help='代理地址示例：http://127.0.0.1:6696')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stdout, level='INFO')
    try:
        flights = await run(date=args.date, origin=args.origin, destination=args.destination, proxy=args.proxy, timeout=args.timeout)
    except Exception as error:
        logger.error('查询失败：{}: {}', type(error).__name__, error)
        return 1
    logger.success('查询成功，共 {} 个航班方案（时间为各机场当地时间）', len(flights))
    show_flights(flights)
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
