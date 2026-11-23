"""Policies come from real document responses and apply to their script sinks."""
import base64
import hashlib

import pytest

pytest.importorskip('pyv8c.pyv8')

from pyv8c.async_api import Browser


@pytest.fixture
async def protected_page():
    state = {'csp': "require-trusted-types-for 'script'; trusted-types default approved; script-src 'unsafe-inline' 'unsafe-eval'"}
    def load(url, request):
        return {'status': 200, 'headers': {'content-type': 'text/html',
                'content-security-policy': state['csp']}, 'body': '<!doctype html><body></body>'}
    browser = await Browser(real_network=False, resource_loader=load, stub_logging='off').__aenter__()
    try:
        page = browser.new_profile().new_page()
        await page.goto('https://policy.test/')
        yield page, state
    finally:
        await browser.close()


async def test_trusted_sinks_reject_plain_strings_without_default(protected_page):
    page, _ = protected_page
    assert page.evaluate('''(() => {
      const div=document.createElement('div'), script=document.createElement('script');
      return [()=>div.innerHTML='<b>x</b>',()=>script.src='data:,0',
              ()=>script.textContent='0',()=>div.setAttribute('onclick','0')].map(f=>{
        try{f();return 'accepted'}catch(e){return e.name}
      });
    })()''') == ['TypeError'] * 4


async def test_default_policy_transforms_sinks_and_receives_type_and_sink(protected_page):
    page, _ = protected_page
    assert page.evaluate('''(() => {
      const calls=[];
      const policy=trustedTypes.createPolicy('default',{
        createHTML:(...args)=>{calls.push(args);return '<b>safe</b>'},
        createScriptURL:()=>'/safe.js'
      });
      const div=document.createElement('div'),script=document.createElement('script');
      div.innerHTML='unsafe';script.src='data:,0';
      return [trustedTypes.defaultPolicy===policy,div.innerHTML,script.getAttribute('src'),calls];
    })()''') == [True, '<b>safe</b>', '/safe.js', [['unsafe', 'TrustedHTML', 'Element innerHTML']]]


async def test_explicit_trusted_values_work_without_default(protected_page):
    page, _ = protected_page
    assert page.evaluate('''(() => {
      const policy=trustedTypes.createPolicy('approved',{
        createHTML:s=>s,createScript:s=>s,createScriptURL:s=>s
      });
      const div=document.createElement('div'),script=document.createElement('script');
      div.innerHTML=policy.createHTML('<b>ok</b>');
      script.src=policy.createScriptURL('/ok.js');
      return [div.innerHTML,script.getAttribute('src'),eval(policy.createScript('2+3')),
              Function(policy.createScript('return 7'))()];
    })()''') == ['<b>ok</b>', '/ok.js', 5, 7]


async def test_policy_allowlist_and_duplicate_name_are_enforced(protected_page):
    page, _ = protected_page
    assert page.evaluate('''(() => {
      trustedTypes.createPolicy('approved',{});
      return ['other','approved'].map(name=>{
        try{trustedTypes.createPolicy(name,{});return 'accepted'}catch(e){return e.name}
      });
    })()''') == ['TypeError', 'TypeError']


async def test_eval_obeys_trusted_types_and_preserves_direct_scope(protected_page):
    page, _ = protected_page
    assert page.evaluate('''(() => {
      let first;try{eval('1+1')}catch(e){first=e.name}
      const policy=trustedTypes.createPolicy('approved',{createScript:s=>s});
      eval(policy.createScript('var localOnly=11'));
      return [first,localOnly,typeof window.localOnly];
    })()''') == ['EvalError', 11, 'undefined']


async def test_default_script_policy_cannot_rewrite_eval_source(protected_page):
    page, _ = protected_page
    assert page.evaluate('''(() => {
      trustedTypes.createPolicy('default',{createScript:s=>'2+3'});
      try{return eval('1+1')}catch(e){return e.name}
    })()''') == 'EvalError'


async def test_missing_unsafe_eval_blocks_even_trusted_script(protected_page):
    page, state = protected_page
    state['csp'] = "script-src 'unsafe-inline'"
    await page.goto('https://policy.test/next')
    assert page.evaluate('''(() => {
      const p=trustedTypes.createPolicy('approved',{createScript:s=>s});
      return [()=>eval('1'),()=>eval(p.createScript('1')),
              ()=>Function(p.createScript('return 1'))()].map(f=>{
        try{f();return 'accepted'}catch(e){return e.name}
      });
    })()''') == ['EvalError'] * 3


async def test_about_blank_frame_inherits_policy_container(protected_page):
    page, _ = protected_page
    assert page.evaluate('''(() => {
      const frame=document.createElement('iframe');document.body.appendChild(frame);
      const child=frame.contentWindow;
      try{child.document.createElement('div').innerHTML='<b>x</b>';return 'accepted'}
      catch(e){return e.name}
    })()''') == 'TypeError'


async def test_meta_policy_applies_to_subsequent_eval(page):
    await page.set_content('''<head><meta http-equiv="content-security-policy"
      content="script-src 'unsafe-inline'"></head><body>
      <script>try{eval('1')}catch(e){window.blocked=e.name}</script></body>''',
      url='https://policy.test/')
    assert page.evaluate('blocked') == 'EvalError'


@pytest.mark.parametrize('algorithm', ['sha256', 'sha384', 'sha512'])
@pytest.mark.parametrize('directive', ['script-src', 'script-src-elem', 'default-src'])
@pytest.mark.parametrize('tampered', [False, True])
async def test_inline_script_obeys_hash_from_meta(page, algorithm, directive, tampered):
    source = '\nwindow.hashResult = "雪😃";\n'
    digest = base64.b64encode(hashlib.new(algorithm, source.encode('utf-8')).digest()).decode('ascii')
    actual = source + (' ' if tampered else '')
    await page.set_content(f'''<!doctype html><head>
      <meta http-equiv="Content-Security-Policy" content="{directive} 'unsafe-inline' '{algorithm}-{digest}'">
      </head><body><script>{actual}</script></body>''', url='https://policy.test/')
    assert page.evaluate('globalThis.hashResult === "雪😃"') is (not tampered)
