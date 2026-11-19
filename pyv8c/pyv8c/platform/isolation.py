"""Document cross-origin isolation established at navigation commit."""
from urllib.parse import urlsplit
import re

from ..web.bom.utils import page_secure_context


def _header(response, name):
    headers = getattr(response, 'headers', None)
    return str(headers.fn_get(name) or '') if headers is not None else ''


def document_isolated(page, url, response):
    if not page_secure_context(page, url) or page.origin[0] == 'opaque':
        return False
    if re.search(r'(?:^|,)\s*cross-origin-isolated\s*=\s*\(\s*\)',
                 _header(response, 'permissions-policy'), re.I):
        return False
    parent = page.parent_page
    if response is None and (str(url).startswith(('about:', 'blob:'))):
        return bool(parent and parent.navigation.cross_origin_isolated)
    coep = _header(response, 'cross-origin-embedder-policy').split(';', 1)[0].strip().lower()
    if coep not in ('require-corp', 'credentialless'):
        return False
    if parent is None:
        coop = _header(response, 'cross-origin-opener-policy').split(';', 1)[0].strip().lower()
        return coop == 'same-origin'
    if not parent.navigation.cross_origin_isolated:
        return False
    # The default allowlist for cross-origin-isolated is self. A frame can
    # delegate it through its container's allow attribute.
    if page.origin == parent.origin:
        return True
    element = getattr(page.window, '_frame_element', None)
    policy = str(getattr(element, 'attributes', {}).get('allow', ''))
    for directive in policy.split(';'):
        parts = directive.strip().split()
        if parts and parts[0].lower() == 'cross-origin-isolated':
            allowed = parts[1:]
            if not allowed or '*' in allowed or "'src'" in allowed:
                return True
            target = urlsplit(str(url))
            origin = f'{target.scheme}://{target.netloc}'
            return origin in allowed
    return False
