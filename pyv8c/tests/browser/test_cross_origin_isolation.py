"""Navigation headers decide isolation, including transitions and worker inheritance."""
import pytest

pytest.importorskip('pyv8c.pyv8')
from pyv8c import Browser


@pytest.fixture
def isolated_page():
    state = {'headers': {'cross-origin-opener-policy': 'same-origin',
                         'cross-origin-embedder-policy': 'require-corp'}}
    def load(url, request):
        return {'status':200, 'body':'<body></body>',
                'headers':{'content-type':'text/html', **state['headers']}}
    browser = Browser(real_network=False, resource_loader=load, stub_logging='off')
    try:
        page=browser.new_profile().new_page()
        page.goto('https://isolated.test/')
        yield page,state
    finally:
        browser.close()


def test_headers_expose_isolated_native_builtins(isolated_page):
    page, _ = isolated_page
    assert page.evaluate('[crossOriginIsolated,typeof SharedArrayBuffer]') == [True,'function']


def test_navigation_loses_and_regains_isolation(isolated_page):
    page,state=isolated_page
    state['headers']={}
    page.goto('https://isolated.test/plain')
    assert page.evaluate('[crossOriginIsolated,typeof SharedArrayBuffer]') == [False,'undefined']
    state['headers']={'cross-origin-opener-policy':'same-origin','cross-origin-embedder-policy':'credentialless'}
    page.goto('https://isolated.test/again')
    assert page.evaluate('[crossOriginIsolated,typeof SharedArrayBuffer]') == [True,'function']


def test_permissions_policy_can_disable_isolation(isolated_page):
    page,state=isolated_page
    state['headers']['permissions-policy']='cross-origin-isolated=()'
    page.goto('https://isolated.test/disabled')
    assert page.evaluate('crossOriginIsolated') is False


def test_blank_frame_and_blob_worker_inherit_isolation(isolated_page):
    page,_=isolated_page
    assert page.evaluate('''(() => {
      const f=document.createElement('iframe');document.body.appendChild(f);
      return [f.contentWindow.crossOriginIsolated,typeof f.contentWindow.SharedArrayBuffer];
    })()''') == [True,'function']
    page.evaluate('''window.answer=null;
      const w=new Worker(URL.createObjectURL(new Blob([
        'postMessage([crossOriginIsolated,typeof SharedArrayBuffer])'
      ],{type:'text/javascript'})));
      w.onmessage=e=>{answer=e.data;w.terminate()};''')
    page.wait_for('answer!==null',timeout=3)
    assert page.evaluate('answer') == [True,'function']


def test_memory_info_is_a_snapshot_and_immediate_reads_share_cache(isolated_page):
    page,_=isolated_page
    assert page.evaluate('''(() => {
      const a=performance.memory, first=a.usedJSHeapSize;
      window.allocation=Array(100000).fill('allocated');
      const b=performance.memory;
      return [a!==b,first===a.usedJSHeapSize,first===b.usedJSHeapSize,
              a.usedJSHeapSize<=a.totalJSHeapSize,a.totalJSHeapSize<a.jsHeapSizeLimit];
    })()''') == [True]*5
