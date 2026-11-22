"""The Navigator members compared with the local Chrome, one probe each.

Both browsers answer the same script from a secure origin (``127.0.0.1``), and
the records have to agree key by key.  Three calls are left out because Chrome
waits for a chooser or an origin-private file system that this environment has
none of -- ``getDisplayMedia``, ``requestDevice`` and ``getDirectory`` -- so
they are asked in the integration suite instead.
"""
from __future__ import annotations

import http.server
import threading

import pytest

pytest.importorskip("pyv8c.pyv8", reason="the comparison runs in the native environment")

from harness import ReferenceUnavailable, chrome_reference, normalize

PAGE = "<!doctype html><html><body><p>probe</p></body></html>"

PROBE = """(async () => {
  const out = {};
  const capture = async (label, fn) => {
    try { out[label] = await fn(); }
    catch (error) { out[label] = error.name + ': ' + error.message; }
  };
  const shape = (name) => {
    const value = navigator[name];
    return value === undefined ? 'undefined' : value.constructor.name;
  };
  out.secure = window.isSecureContext;
  out.shape = ['mediaDevices', 'permissions', 'mediaSession', 'serviceWorker',
               'credentials', 'bluetooth', 'storage'].map(shape);
  out.javaEnabled = navigator.javaEnabled();
  await capture('status', async () => {
    const status = await navigator.permissions.query({name: 'geolocation'});
    return [status.constructor.name, status.name, status.state, status.onchange === null,
            PermissionStatus.prototype.isPrototypeOf(status)];
  });
  await capture('query.promise', () => {
    const pending = navigator.permissions.query({name: 'geolocation'});
    return [pending instanceof Promise, typeof pending.then];
  });
  await capture('states', async () => {
    const names = ['notifications', 'camera', 'midi', 'clipboard-read', 'clipboard-write',
                   'background-sync', 'periodic-background-sync', 'persistent-storage',
                   'window-management'];
    const states = {};
    for (const name of names) states[name] = (await navigator.permissions.query({name})).state;
    return states;
  });
  await capture('query.unknown', () => navigator.permissions.query({name: 'nope'}));
  await capture('query.push', () => navigator.permissions.query({name: 'push'}));
  await capture('query.noargs', () => navigator.permissions.query());
  await capture('query.newObject', async () => {
    const first = await navigator.permissions.query({name: 'geolocation'});
    return first !== await navigator.permissions.query({name: 'geolocation'});
  });
  await capture('constraints', () => {
    const supported = navigator.mediaDevices.getSupportedConstraints();
    const on = Object.keys(supported).filter((key) => supported[key]).sort();
    return [on.length, on.slice(0, 3), on.includes('torch'), 'nonsense' in supported];
  });
  await capture('devices', async () => {
    const devices = await navigator.mediaDevices.enumerateDevices();
    return [devices.length, devices[0].constructor.name, JSON.stringify(devices[0]),
            devices[0].toJSON().kind, Object.getPrototypeOf(devices[0]).constructor.name];
  });
  await capture('getUserMedia', () => navigator.mediaDevices.getUserMedia({audio: true}));
  await capture('getUserMedia.empty', () => navigator.mediaDevices.getUserMedia());
  await capture('getUserMedia.false', () => navigator.mediaDevices.getUserMedia({video: false}));
  await capture('session.defaults', () => [
      String(navigator.mediaSession.metadata), navigator.mediaSession.playbackState,
      MediaSession.prototype.isPrototypeOf(navigator.mediaSession)]);
  await capture('session.metadata', () => {
    const metadata = new MediaMetadata({title: 'T', artist: 'A', album: 'B',
        artwork: [{src: 'a.png', sizes: '1x1', type: 'image/png'}]});
    navigator.mediaSession.metadata = metadata;
    const back = navigator.mediaSession.metadata;
    return [back === metadata, back.constructor.name, back.title, back.artist, back.album,
            back.artwork.length, back.artwork[0].src, back.artwork[0].sizes, back.chapterInfo.length,
            Object.keys(back).length, back.artwork === back.artwork,
            back.chapterInfo === back.chapterInfo];
  });
  await capture('session.defaults.metadata', () => {
    const metadata = new MediaMetadata();
    return [metadata.title, metadata.artist, metadata.album, metadata.artwork.length,
            metadata.chapterInfo.length];
  });
  await capture('session.plain', () => { navigator.mediaSession.metadata = {title: 'x'}; });
  await capture('session.cleared', () => {
    navigator.mediaSession.metadata = null;
    return String(navigator.mediaSession.metadata);
  });
  await capture('session.playback', () => {
    navigator.mediaSession.playbackState = 'playing';
    const state = navigator.mediaSession.playbackState;
    navigator.mediaSession.playbackState = 'nonsense';
    return [state, navigator.mediaSession.playbackState];
  });
  await capture('session.handlers', () => {
    navigator.mediaSession.setActionHandler('play', () => {});
    navigator.mediaSession.setActionHandler('play', null);
    return ['play', 'nexttrack', 'enterpictureinpicture'].map((action) => {
      try { navigator.mediaSession.setActionHandler(action, null); return 'ok'; }
      catch (error) { return error.name; }
    });
  });
  await capture('session.handler.bad', () => navigator.mediaSession.setActionHandler('play', 1));
  await capture('session.handler.invalid',
      () => navigator.mediaSession.setActionHandler('nope', null));
  await capture('session.position', () => navigator.mediaSession.setPositionState(
      {duration: 10, playbackRate: 1, position: 5}));
  await capture('session.position.reset', () => navigator.mediaSession.setPositionState());
  await capture('session.position.bad', () => navigator.mediaSession.setPositionState(
      {duration: 10, position: 20}));
  await capture('session.position.duration',
      () => navigator.mediaSession.setPositionState({duration: -1}));
  await capture('worker.shape', () => [
      String(navigator.serviceWorker.controller), navigator.serviceWorker.ready instanceof Promise,
      String(navigator.serviceWorker.oncontrollerchange),
      typeof navigator.serviceWorker.register]);
  await capture('worker.registration',
      async () => String(await navigator.serviceWorker.getRegistration('/')));
  await capture('worker.registrations',
      async () => (await navigator.serviceWorker.getRegistrations()).length);
  await capture('worker.startMessages', () => navigator.serviceWorker.startMessages());
  await capture('worker.register.crossorigin',
      () => navigator.serviceWorker.register('http://other.test/sw.js'));
  await capture('worker.register.empty', () => navigator.serviceWorker.register(''));
  await capture('worker.register.html', () => navigator.serviceWorker.register('/sw.js'));
  await capture('credentials.get', () => navigator.credentials.get({password: true}));
  await capture('credentials.federated',
      () => navigator.credentials.get({federated: {providers: []}}));
  await capture('credentials.noargs', () => navigator.credentials.get());
  await capture('credentials.prevent', () => navigator.credentials.preventSilentAccess());
  await capture('credentials.store', () => navigator.credentials.store({}));
  await capture('credentials.create', () => navigator.credentials.create({}));
  await capture('credentials.create.password',
      () => navigator.credentials.create({password: {}}));
  await capture('bluetooth.available', () => navigator.bluetooth.getAvailability());
  await capture('storage.estimate', async () => {
    const estimate = await navigator.storage.estimate();
    return [typeof estimate.quota, typeof estimate.usage, Object.keys(estimate).sort(),
            estimate.usage >= 0, estimate.quota > 0];
  });
  await capture('storage.persisted', () => navigator.storage.persisted());
  await capture('storage.persist', () => navigator.storage.persist());
  return out;
})()"""

RECORDS: list[dict] = []
LOCK = threading.Lock()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self):
        with LOCK:
            RECORDS.append(self.path)
        content_type = "text/html" if self.path == "/sw.js" else "text/html; charset=utf-8"
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self): self._handle()
    def log_message(self, *_args): pass


@pytest.fixture(scope="module")
async def compared():
    """The same probe, answered by both browsers from one secure origin."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/doc"
    try:
        try:
            async with chrome_reference() as reference:
                chrome, device_kinds = await _chrome_probe(reference, url)
        except ReferenceUnavailable as unavailable:
            pytest.skip(str(unavailable))

        from pyv8c.async_api import Browser

        browser = (await Browser(real_network=True, stub_logging="off",
                                 environment={"media.devices": device_kinds}).__aenter__())
        try:
            page = browser.new_profile().new_page()
            (await page.goto(url, wait_until="load"))
            page.evaluate("(() => { " + PROBE + ".then((value) => { window.__probe = value; },"
                          " (error) => { window.__probe = {rejected: String(error.name)}; }); })()")
            (await page.wait_for("typeof window.__probe !== 'undefined'", timeout=5))
            mine = normalize(page.evaluate("window.__probe") or {})
        finally:
            (await browser.close())
        yield {"chrome": chrome, "pyv8c": mine}
    finally:
        server.shutdown()
        server.server_close()


async def _chrome_probe(reference, url):
    page = (await reference.browser.new_page())
    try:
        (await page.goto(url, wait_until="load"))
        # The kind list is a host input. Compare API shape and conversion with
        # the same inputs even when machines have different audio hardware.
        device_kinds = await page.evaluate(
            "async () => (await navigator.mediaDevices.enumerateDevices()).map(device => device.kind)"
        )
        return (await page.evaluate(PROBE)) or {}, device_kinds
    finally:
        (await page.close())


async def test_the_probe_answered(compared):
    assert compared["pyv8c"], "pyv8c recorded nothing"
    assert compared["chrome"], "Chrome recorded nothing"


async def test_every_member_answers_the_same(compared):
    mine, theirs = compared["pyv8c"], compared["chrome"]
    report = "\n".join(f"{key}:\n  pyv8c: {mine.get(key)!r}\n  chrome: {theirs[key]!r}"
                       for key in theirs if mine.get(key) != theirs[key])
    assert report == "", report
