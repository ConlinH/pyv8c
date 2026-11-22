"""Convert between resource payloads and the Web Response on their owner."""
from ...foundation.host_errors import LoadFailure
from ...network.models import NetworkResponse
from ...web.fetch.response import Response
from .local import LocalExtensionResponse


def plain_response(value, url, *, allow_stream=False):
    """Copy response values out of their transport or host representation."""
    if isinstance(value, dict):
        body = value.get('body', '')
        headers = value.get('headers', {})
        status, status_text = value.get('status', 200), value.get('statusText', 'OK')
        redirected, kind = value.get('redirected', False), value.get('type', 'basic')
        redirect_crossed = value.get('redirectCrossed', False)
        url = value.get('url') or url
    elif isinstance(value, (str, bytes)) or value is None:
        body, headers, status, status_text, redirected, kind = str(value) if value is None else value, {}, 200, 'OK', False, 'basic'
        redirect_crossed = False
    else:
        body, headers = value.body, value.headers
        headers = getattr(headers, '_data', headers)
        status, status_text = value.status, value.status_text
        redirected, kind = value.redirected, value.type
        redirect_crossed = getattr(value, 'redirect_crossed',
                                   getattr(value, '_redirect_crossed', False))
        url = value.url or url
    if isinstance(body, (bytearray, memoryview)):
        body = bytes(body)
    if body is not None and not isinstance(body, (str, bytes)):
        from ...web.bom.compression_streams import ReadableStream
        if not allow_stream or not isinstance(body, ReadableStream):
            raise LoadFailure('resource response body is not plain data')
    if not isinstance(headers, dict):
        raise LoadFailure('resource response headers are not plain data')
    normalized = {}
    for name, item in headers.items():
        if isinstance(item, (list, tuple)) and all(isinstance(part, str) for part in item):
            item = list(item)
        elif not isinstance(item, str):
            raise LoadFailure('resource response headers are not plain data')
        if not isinstance(name, str):
            raise LoadFailure('resource response headers are not plain data')
        normalized[name.lower()] = item
    return NetworkResponse(url=str(url), body=body, status=int(status), status_text=str(status_text),
                           headers=normalized,
                           redirected=bool(redirected), type=str(kind),
                           redirect_crossed=bool(redirect_crossed))


def loader_response(value, target, *, allow_stream=False):
    if isinstance(value, dict):
        value = Response(value.get('body', ''), {**value, 'url': target})
    elif isinstance(value, (str, bytes)) or value is None:
        return NetworkResponse(url=target, body=str(value) if value is None else value,
                               status_text='', type='default')
    return plain_response(value, target, allow_stream=allow_stream)


def web_response(raw, services):
    """Build the Web-facing response on the receiving Realm's owner."""
    response = Response(raw.body, {
        'status': raw.status, 'statusText': raw.status_text, 'url': raw.url,
        'headers': raw.headers, 'redirected': raw.redirected, 'type': raw.type,
    }, services=services)
    response._redirect_crossed = raw.redirect_crossed
    response._redirect_hops = raw.redirect_hops
    response._redirect_span = raw.redirect_span
    response._local_extension = isinstance(raw, LocalExtensionResponse)
    return response
