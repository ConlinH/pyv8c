"""Local resource resolvers: no transport and no redirect machinery."""
import base64
from urllib.parse import unquote_to_bytes
from ...foundation.environment import extension_resource
from ...foundation.host_errors import LoadFailure
from ...foundation.logging import logger
from ...foundation.urls import origin_tuple, unloadable_scheme
from ...network.models import NetworkResponse

#: how the load completes (an image completes as one so its element answers for the size).
PRELOAD_KINDS = {
    "image": ("image", "image", "img"),
    "audio": ("media", "audio", "preload"),
    "video": ("media", "video", "preload"),
    "font": ("font", "font", "preload"),
    "script": ("script", "script", "preload"),
    "style": ("style", "style", "preload"),
    "fetch": ("fetch", "fetch", "preload"),
    "track": ("track", "track", "preload"),
}

#: The kind of file each suffix promises; an unknown suffix is served as bytes.
EXTENSION_TYPES = {
    "css": "text/css", "js": "text/javascript", "mjs": "text/javascript",
    "json": "application/json", "html": "text/html", "htm": "text/html",
    "txt": "text/plain", "svg": "image/svg+xml",
    "png": "image/png", "gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "bmp": "image/bmp", "ico": "image/x-icon",
}

#: A one-pixel PNG, and a small SVG: an image that decodes, for a path promising one.
_EXTENSION_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/QFz7GjWAAAAAElFTkSuQmCC"
)
_EXTENSION_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"></svg>'


class LocalExtensionResponse(NetworkResponse):
    """Payload produced by the local resolver for an authorized extension."""


def validate_source_scheme(environment, initiator, target, *, mode, browser_initiated):
    """Reject browser-owned schemes before cache access or host interception."""
    blocked = unloadable_scheme(target)
    if blocked is None:
        return None
    if str(target).lower().startswith('chrome-extension:') and (mode != 'navigate' or browser_initiated):
        resource = extension_resource(environment, target, source_url=initiator)
        if resource is not None:
            return resource
    raise LoadFailure(blocked)


def resolve_local_response(profile, target, origin, mode, extension_path):
    if extension_path is not None:
        return _extension_response(target, extension_path)
    if target.startswith('about:blank'):
        return NetworkResponse(url=target)
    if target.startswith('blob:'):
        inherited = profile._blob_origins.get(target)
        target_origin = origin_tuple(target) if inherited is None else tuple(inherited)
        if mode != 'navigate' and origin != target_origin:
            raise LoadFailure('Blob URL belongs to another origin')
        body, content_type = profile.resolve_blob_url(target)
        return NetworkResponse(url=target, body=body, headers={'content-type': content_type})
    if target[:5].lower() == 'data:':
        return _data_url_response(target)
    return None


def _data_url_response(url):
    """The response a ``data:`` URL carries, decoded in place.

    A ``data:`` URL *is* its payload: Chromium fetches nothing for it, and a frame whose
    ``src`` is one gets its document without waiting on an answer no network would give.
    """
    header, comma, payload = str(url)[len("data:"):].partition(",")
    if not comma:
        raise LoadFailure("data: URL carries no payload")
    if header.endswith(";base64"):
        header = header[: -len(";base64")]
        try:
            body = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=False)
        except ValueError as exc:  # binascii.Error is one
            raise LoadFailure(f"data: URL payload is not base64: {exc}") from None
    else:
        body = unquote_to_bytes(payload)
    content_type = header or "text/plain;charset=US-ASCII"
    return NetworkResponse(url=str(url), body=body, status_text='', type='default',
                           headers={"content-type": content_type})


def _extension_response(target, resource):
    """Build the placeholder for an allowed extension resource.

    Chrome answers one locally, in the file's own kind, and a page whose origin the
    extension's ``matches`` names reads it without a CORS check. This framework has
    declarations but not extension files: the returned contents are placeholders,
    not the extension's actual resources.
    """
    logger.warning("extension resource is a placeholder, not extension content: {}", target)
    content_type = EXTENSION_TYPES.get(resource.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    if content_type == "image/svg+xml":
        body = _EXTENSION_SVG
    elif content_type.startswith("image/"):
        body = _EXTENSION_PNG
    else:
        body = b""
    return LocalExtensionResponse(url=target, body=body, status_text='',
                                  headers={"content-type": content_type, "content-length": str(len(body))})


