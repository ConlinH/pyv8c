"""用 pyv8c 获取 Reese84 Cookie，再查询 Nevada 注册代理信息。"""
import argparse
import asyncio
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import pyv8
from pyv8c.async_api import Browser

ORIGIN = 'https://orion.nv.gov'
URL = ORIGIN + '/portal/public/#/public/nvsos/en/CaseXscreen?screen=external-GenericFilingsSearch&tabRoute=agent'
SDK_PATH = '/sworthe-which-welcolmembling-those-That-th-edgem'
API_URL = ORIGIN + '/core/api/_q/v1/nv-sos/public/CRAEntities'
DEFAULT_PROXY = 'http://127.0.0.1:6696'


def trim_resources(url, request):
    target, method = urlsplit(url), request.get('method', 'GET').upper()
    if target.scheme not in {'http', 'https'}:
        return None
    allowed = target.hostname == 'orion.nv.gov' and (
        (target.path == '/portal/public/' and method == 'GET')
        or (target.path == SDK_PATH and method in {'GET', 'POST'})
        or (target.path == '/_Incapsula_Resource' and method == 'GET'
            and {'SWJIYLWA', 'SWKMTFSR'}.intersection(parse_qs(target.query)))
    )
    return None if allowed else False


def search_body(keyword):
    params = {'agentName': keyword, 'registeredAgentType': '', 'craId': '', 'nvBusinessId': '',
              'craEntityNumber': '', 'agentStatus': '', 'filingNo': '', 'submit1': False,
              'searchDumy': True, 'restrictedAndProhibitedWords': '', 'totalRecordCount': '',
              'searchType': 'STARTS_WITH', 'searchTerm': ''}
    return {'data': {}, 'origin': '', 'origin_key': 'CaseX',
            'gridInput': {'params': {'parameters': params.copy()}, 'persistPrevParams': False},
            'parameters': params, 'pageNo': 1, 'pageSize': 25, 'sortBy': {}}


async def run(*, keyword='NORTHWEST', proxy=DEFAULT_PROXY, timeout=60):
    started = time.monotonic()
    logger.info('查询 {}（代理：{}）', keyword, proxy or '直连')
    async with asyncio.timeout(timeout), Browser(
        proxy=proxy, cache=False, resource_interceptor=trim_resources,
        http_options={'impersonate': 'chrome150', 'timeout': 20},
    ) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        await page.goto(URL, wait_until='domcontentloaded', timeout=timeout)
        while not any(part.startswith('reese84=') and part[8:]
                      for part in profile.cookies.header_for(API_URL).split('; ')):
            await page.sleep(.2)
        logger.info('Reese84 Cookie 已就绪，请求业务接口')
        reese84 = next(part for part in profile.cookies.header_for(API_URL).split('; ')
                       if part.startswith('reese84=') and part[8:])
        logger.info('{}', reese84)
        response = await page.net.post(API_URL, json=search_body(keyword), timeout=20)
        if response.status != 200:
            raise RuntimeError(f'查询接口 HTTP {response.status}: {response.text[:200]}')
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise RuntimeError('查询接口未返回 items 列表')
        return {'data': data, 'elapsed': round(time.monotonic() - started, 2)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keyword', default='NORTHWEST')
    parser.add_argument('--proxy', default=DEFAULT_PROXY, help='代理地址；传 none 表示直连')
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout 必须为正数')
    proxy = None if args.proxy.lower() in {'none', ''} else args.proxy
    logger.remove()
    logger.add(sys.stdout, level='INFO')
    try:
        result = await run(keyword=args.keyword, proxy=proxy, timeout=args.timeout)
    except (Exception, pyv8.JSException, pyv8.JavaScriptTerminated) as error:
        logger.error('查询失败：{}: {}', type(error).__name__, error)
        return 1
    data = result['data']
    logger.success('查询成功，code={}，total={}（耗时 {}s）',
                   data.get('code'), data.get('totalItemCount'), result['elapsed'])
    for item in data['items'][:5]:
        logger.info('{}', item.get('agentname'))
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
