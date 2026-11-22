"""Local DOM lookup and named-property rules on both runtimes."""
import pytest

pytest.importorskip('pyv8c.pyv8')
from harness import chrome_reference, ReferenceUnavailable
from pyv8c.async_api import Browser


@pytest.fixture(scope='module')
async def reference():
    try:
        async with chrome_reference() as session:
            yield session
    except ReferenceUnavailable as exc:
        pytest.skip(str(exc))


CASES = (
    '''() => {
        document.body.innerHTML='<div id="x"></div>';
        let el=document.getElementById('x'),a=el.attributes.getNamedItem('id');
        el.id='y';let updated=a.value==='y';
        let same=el.attributes.getNamedItem('id')===a;
        let removed=el.attributes.removeNamedItem('id');el.id='new';a.value='detached';
        return [updated,same,removed===a,a.ownerElement===null,el.id==='new',
            document.getElementById('detached')===null];
    }''',
    '''() => {
        let a=document.createElement('i'), b=document.createElement('i');
        a.id=b.id='x';document.body.append(a,b);
        let before=document.getElementById('x')===a;
        document.body.insertBefore(b,a); let reordered=document.querySelector('#x')===b;
        b.attributes.getNamedItem('id').value='y';
        a.remove();let removed=document.getElementById('x')===null;
        document.body.appendChild(a);
        return [before,reordered,removed,document.getElementById('y')===b,
            document.getElementById('')===null,document.getElementById('x')===a];
    }''',
    '''() => {
        let f=document.createDocumentFragment(), host=document.createElement('div');
        let el=document.createElement('span');el.id='x';f.appendChild(el);
        let detached=f.getElementById('x')===el && document.getElementById('x')===null;
        document.body.append(f,host);let r=host.attachShadow({mode:'open'});
        r.innerHTML='<i id="x"></i>';
        return [detached,f.getElementById('x')===null,document.getElementById('x')===el,
            r.getElementById('x')!==el,r.querySelector('#x')===r.getElementById('x')];
    }''',
    '''() => {
        document.body.innerHTML='<i id="length"></i><i id="item"></i><i id="foo"></i>';
        let c=document.getElementsByTagName('i'); HTMLCollection.prototype.foo='prototype';
        let values=[c.length,typeof c.item,typeof c.namedItem,c.namedItem('length').id,
            c.foo, 'length' in c, Object.getOwnPropertyDescriptor(c,'length')===undefined];
        c.length=9;values.push(c.length,delete c.length);
        delete HTMLCollection.prototype.foo;values.push(c.foo.id);
        return values;
    }''',
)


@pytest.mark.parametrize('script', CASES, ids=['attribute-lifetime','ids','roots','collections'])
async def test_local_lookup_rules(reference, script):
    chrome=(await reference.browser.new_page())
    browser=(await Browser(real_network=False,stub_logging='off').__aenter__())
    try:
        (await chrome.set_content('<body></body>'))
        expected=(await chrome.evaluate(script))
        page=browser.new_profile().new_page();(await page.set_content('<body></body>', wait_until="commit"))
        actual=page.evaluate('('+script+')()')
        assert actual == expected
    finally:
        (await chrome.close())
        (await browser.close())
