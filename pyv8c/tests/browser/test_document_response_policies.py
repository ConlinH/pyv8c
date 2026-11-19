"""Response policies affect live requests and permissions rather than profile defaults."""
import pytest

pytest.importorskip('pyv8c.pyv8')
from pyv8c import Browser


def test_same_origin_referrer_policy_omits_cross_origin_referrer_and_document_referrer():
    requests=[]
    def loader(url,request):
        requests.append((url,request))
        return {'status':200,'headers':{'content-type':'text/html','referrer-policy':'same-origin'},
                'body':'<body><iframe src="https://other.test/frame"></iframe></body>' if url.endswith('/page') else '<body>child</body>'}
    browser=Browser(real_network=False,resource_loader=loader,stub_logging='off')
    try:
        page=browser.new_profile().new_page();page.goto('https://policy.test/page')
        page.wait_for('frames.length===1',timeout=3)
        frame=page.executor.call(lambda:page.document.fn_querySelectorAll('iframe')[0]._child_services._page_realm.page)
        assert frame.evaluate('document.referrer') == ''
        sent=next(r for url,r in requests if url=='https://other.test/frame')
        assert 'referer' not in {k.lower() for k in sent['headers']}
    finally:browser.close()


def test_permissions_policy_denials_override_prompt_state():
    browser=Browser(real_network=False,resource_loader=lambda u,r:{'status':200,'body':'<body></body>',
      'headers':{'permissions-policy':'camera=(), microphone=(), geolocation=()'}},stub_logging='off')
    try:
        page=browser.new_profile().new_page();page.goto('https://policy.test/')
        page.evaluate("window.result=null; Promise.all(['camera','microphone','geolocation'].map(name=>navigator.permissions.query({name}).then(s=>s.state))).then(v=>result=v)")
        page.wait_for('result!==null',timeout=3)
        assert page.evaluate('result') == ['denied']*3
        assert page.evaluate("[document.featurePolicy.allowsFeature('camera'),document.featurePolicy.getAllowlistForFeature('camera')]") == [False,[]]
    finally:browser.close()
