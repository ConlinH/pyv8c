"""Click the live SteamDB challenge and verify the resulting login HTML.

No Chrome, captured Cookie, HAR replay, or saved challenge parameters are used.
HTML stays in memory; the CLI prints verification details without saving files.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pyv8c'))

from loguru import logger
from pyv8c import Browser, pyv8, register_transport
from pyv8c.foundation.arguments import dictionary
from pyv8c.foundation.host_errors import HostStateError, RealmBusyError
from pyv8c.network.curl_cffi import CurlCffiTransport
from pyv8c.web.dom.hit_testing import border_box
from pyv8c.web.dom.nodes import _subtree_nodes


URL = 'https://steamdb.info/login/'
# URL = 'https://pastebin.com/login'
# URL = 'https://www.e-food.gr'
# URL = "https://fractal-testnet.unisat.io/explorer"

# URL = "https://cn.airbusan.com/content/individual"
# URL = "https://simple.ripley.com.pe/"

HOST_NAME = urlsplit(URL).hostname
PROXY = 'http://127.0.0.1:6696'
# PROXY = None


class _LoginDocumentParser(HTMLParser):
    """Read text and links, excluding executable and non-visible content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts = []
        self.title_parts = []
        self.links = []
        self.buttons = []
        self.paragraphs = []
        self._skip = None
        self._title = False
        self._link = None
        self._button = None
        self._paragraph = None

    def handle_starttag(self, tag, attributes):
        if self._skip:
            return
        if tag in {'script', 'style', 'template'}:
            self._skip = tag
        elif tag == 'title':
            self._title = True
        elif tag == 'a':
            self._link = {'href': dict(attributes).get('href', ''), 'parts': []}
        elif tag == 'button':
            self._button = []
        elif tag == 'p':
            self._paragraph = []

    def handle_endtag(self, tag):
        if self._skip:
            if tag == self._skip:
                self._skip = None
            return
        if tag == 'title':
            self._title = False
        elif tag == 'a' and self._link is not None:
            self.links.append((self._link['href'], ' '.join(self._link['parts']).split()))
            self._link = None
        elif tag == 'button' and self._button is not None:
            self.buttons.append(' '.join(' '.join(self._button).split()))
            self._button = None
        elif tag == 'p' and self._paragraph is not None:
            self.paragraphs.append(' '.join(' '.join(self._paragraph).split()))
            self._paragraph = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)
        if self._link is not None:
            self._link['parts'].append(data)
        if self._button is not None:
            self._button.append(data)
        if self._paragraph is not None:
            self._paragraph.append(data)


def inspect_login_html(html, url=URL):
    """Require the login copy and Steam sign-in button or link from the reference."""
    parser = _LoginDocumentParser()
    parser.feed(html)
    parser.close()
    text = ' '.join(' '.join(parser.text_parts).split())
    normalized = text.casefold()
    phrases = {
        'login_intro': 'Logging into SteamDB allows you to access additional features.',
        'public_account_info': 'we will fetch public information about your account',
        'valve_disclaimer': 'This website is not affiliated with Valve Corporation or Steam.',
        'session_cookie_notice': 'We use cookies to keep your session active.',
        'password_notice': 'SteamDB never receives your username or password.',
    }
    checks = {name: phrase.casefold() in normalized for name, phrase in phrases.items()}
    sign_in_url = None
    for href, words in parser.links:
        label = ' '.join(words).casefold()
        link = urljoin(url, href)
        target = urlsplit(link)
        if ('sign in through' in label and 'steam' in label and href
                and target.scheme == 'https'
                and target.hostname in {HOST_NAME, 'steamcommunity.com'}):
            sign_in_url = link
            break
    button_text = next((label for label in parser.buttons
                        if 'sign in through' in label.casefold()
                        and 'steam' in label.casefold()), None)
    checks['steam_sign_in_control'] = sign_in_url is not None or button_text is not None
    login_paragraphs = [paragraph for paragraph in parser.paragraphs
                        if any(phrase.casefold() in paragraph.casefold()
                               for phrase in phrases.values())]
    return {'matches_reference': all(checks.values()), 'checks': checks,
            'title': ' '.join(''.join(parser.title_parts).split()),
            'steam_sign_in_url': sign_in_url, 'steam_sign_in_button': button_text,
            'text': text, 'login_paragraphs': login_paragraphs}


def inspect_login_response(request, response):
    """Inspect a top-level login document, including the challenge's form POST."""
    target = urlsplit(response.url)
    if (request.method.upper() not in {'GET', 'POST'}
            or request.mode != 'navigate' or not request.top_level_navigation
            or target.scheme != 'https' or target.hostname != HOST_NAME
            or target.path != urlsplit(URL).path):
        return None
    is_html = 'text/html' in response.headers.get('content-type', '').lower()
    document = inspect_login_html(response.text, response.url) if is_html else None
    return {'ok': bool(response.status == 200 and document and document['matches_reference']),
            'method': request.method.upper(), 'status': response.status,
            'document': document}


def run(*, proxy=PROXY, timeout=90):
    started = time.monotonic()
    deadline = started + timeout
    received = threading.Event()
    login_received = threading.Event()
    answer = {}
    last_document = {}
    clicked = set()
    observed_documents = set()
    challenge_errors = []

    def observe_challenge(event):
        if event.get_origin() != 'https://challenges.cloudflare.com':
            return
        data = event.get_data()
        if not isinstance(data, (dict, pyv8.JSObject)):
            return
        data = dictionary(data)
        if (data.get('source') == 'cloudflare-challenge' and data.get('event') == 'fail'):
            code = str(data.get('code', 'unknown'))
            challenge_errors.append(code)
            logger.warning('Turnstile 验证失败，code={}', code)

    class ObservedTransport(CurlCffiTransport):
        def _call(self, request):
            response = super()._call(request)
            value = response.cookies.get('cf_clearance')
            if (response.status == 200 and value
                    and urlsplit(response.url).hostname == HOST_NAME):
                answer.update(clearance_status=response.status, clearance_url=response.url,
                              set_cookie=response.headers['set-cookie'], cf_clearance=value)
                received.set()
            verification = inspect_login_response(request, response)
            if verification is not None:
                document = verification['document']
                last_document.update(status=response.status,
                                     method=verification['method'],
                                     checks=document['checks'] if document else {})
                logger.info('登录页 {} HTTP {}，{} bytes',
                            verification['method'], response.status, len(response.content))
                if verification['ok']:
                    answer.update(ok=True, status=response.status, url=response.url,
                                  method=verification['method'],
                                  content_type=response.headers.get('content-type', ''),
                                  html=response.text, html_bytes=len(response.content),
                                  html_sha256=hashlib.sha256(response.content).hexdigest(),
                                  document=document)
                    login_received.set()
            return response

    register_transport('5s-observed', ObservedTransport, proxy=True)
    browser = Browser(cache=False, threaded_loaders=True, proxy=proxy, gpu_enabled=False,
                      http_engine='5s-observed',
                      http_options={'impersonate': 'chrome150', 'timeout': 20, 'retry': 5})
    try:
        profile = browser.new_profile()
        page = profile.new_page()
        page.goto(URL, wait_until='domcontentloaded', timeout=min(30, timeout))

        def click_ready_control():
            frames, pending = [], [page]
            while pending:
                frame = pending.pop()
                if frame.closed:
                    continue
                frames.append(frame)
                pending.extend(tuple(frame._owned.frames))
            for frame in frames:
                if frame.document not in observed_documents:
                    frame.window.fn_addEventListener('message', observe_challenge)
                    observed_documents.add(frame.document)
                target = urlsplit(frame.url)
                if target.hostname != 'challenges.cloudflare.com' or '/turnstile/f/' not in target.path:
                    continue
                # The widget uses a closed shadow tree. Inspect host DOM objects
                # on their owner thread, without altering the SDK or shadow mode.
                for node in _subtree_nodes(frame.document):
                    if (node in clicked or getattr(node, 'localName', '') != 'input'
                            or node.attributes.get('type') != 'checkbox'):
                        continue
                    box = border_box(node)
                    if box.width <= 0 or box.height <= 0:
                        continue
                    try:
                        frame.mouse.click_element(node, delay=.08)
                    except HostStateError:
                        continue  # A covered/hidden control is not ready yet.
                    clicked.add(node)
                    logger.info('已点击验证复选框，第 {} 次', len(clicked))

        while not login_received.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'No matching HTTP 200 login HTML within {timeout:g}s; '
                                   f'clicks={len(clicked)}, clearance={received.is_set()}, '
                                   f'challenge_errors={challenge_errors}, '
                                   f'last_document={last_document}')
            try:
                page.executor.call(click_ready_control, timeout=min(3, remaining))
            except RealmBusyError:
                # The SDK may be processing a response; the transport still
                # observes replies without enqueuing JS evaluation probes.
                pass
            login_received.wait(min(.25, max(0, deadline-time.monotonic())))

        answer.update(clicks=len(clicked), clearance_received=received.is_set(),
                      challenge_errors=challenge_errors,
                      elapsed=round(time.monotonic()-started, 2),
                      cookie_jar_held='cf_clearance=' in profile.cookies.header_for(URL))
        return answer
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', default=PROXY)
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    logger.remove()
    logger.add(sys.stderr, level='INFO', format='{time:HH:mm:ss} | {level:<7} | {message}',
               filter=lambda record: record['name'] == __name__)
    try:
        result = run(proxy=args.proxy, timeout=args.timeout)
    except Exception as error:
        logger.error('验收失败：{}: {}', type(error).__name__, error)
        return 1
    logger.success('登录页验收通过：{} {} → HTTP {}', result['method'], result['url'], result['status'])
    logger.info('页面标题：{}；正文 {} bytes；Content-Type={}', result['document']['title'],
                result['html_bytes'], result['content_type'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

