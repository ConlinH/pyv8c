"""Explicit 145 baseline; iterable semantics are independent of profile shape."""
from pathlib import Path
import pytest
from pyv8c.async_api import Browser

IDL = Path(__file__).resolve().parents[2] / "idls" / "145.0.7632.162.zip"

CASES = [
    ("cross-realm-next", """() => {
        const f=document.createElement('iframe');document.body.appendChild(f);
        const q=new f.contentWindow.URLSearchParams('b=2');
        const next=Object.getPrototypeOf(q.entries()).next;
        return [new URLSearchParams('a=1').entries(),new Headers([['c','3']]).entries(),{}].map(i=>{
            try { const r=next.call(i);return [r.done,r.value]; } catch(e) { return e.name; }
        });
    }"""),
    ("params", """() => {
        const p=new URLSearchParams('a=1&b=2'), i=p.entries();
        const first=i.next().value;p.delete('a');p.append('c','3');
        const remaining=Array.from(i); const done=i.next().done;p.append('d','4');
        let calls=[];p.forEach(function(v,k,s){calls.push([v,k,s===p,this.tag])},{tag:1});
        return [first,remaining,done,i.next().done,calls,p[Symbol.iterator]===p.entries];
    }"""),
    ("headers", """() => {
        const h=new Headers([['z','1'],['a','2']]);let calls=[];
        h.forEach(function(v,k,s){calls.push([v,k,s===h,this.tag])},{tag:1});
        return [Array.from(h),Array.from(h.keys()),Array.from(h.values()),calls,h[Symbol.iterator]===h.entries];
    }"""),
    ("formdata", """() => {
        const f=new FormData();f.append('a','1');f.append('a','2');
        let calls=[];f.forEach((v,k,s)=>{calls.push([v,k,s===f]);if(calls.length===1)f.append('b','3')});
        return [Array.from(f),Array.from(f.keys()),calls,f[Symbol.iterator]===f.entries];
    }"""),
    ("fonts", """() => {
        document.fonts.clear();const a=new FontFace('A','url(a)'), b=new FontFace('B','url(b)'), c=new FontFace('C','url(c)');
        document.fonts.add(a);document.fonts.add(b);let i=document.fonts.values();let first=i.next().value===a;
        document.fonts.delete(a);document.fonts.add(c);let rest=Array.from(i).map(f=>f.family);
        let done=i.next().done;document.fonts.add(a);
        let calls=[];document.fonts.forEach(function(v,k,s){calls.push([v.family,v===k,s===document.fonts,this.tag])},{tag:1});
        return [first,rest,done,i.next().done,calls,document.fonts[Symbol.iterator]===document.fonts.values,
            Array.from(document.fonts.keys()).map(f=>f.family)];
    }"""),
    ("nodes", """() => {
        document.body.innerHTML='<i></i><b></b>';const n=document.querySelectorAll('*');
        return [n[Symbol.iterator]===n.values,Array.from(n.keys()),Array.from(n.entries()).map(p=>p[0]),
            typeof document.getElementsByTagName('*').keys];
    }"""),
    ("style", """() => {
        const el=document.createElement('div');el.style.setProperty('color','red');const m=el.attributeStyleMap;
        let calls=[];m.forEach(function(v,k,s){calls.push([k,v.toString(),s===m,this.tag])},{tag:1});
        return [Array.from(m.keys()),Array.from(m).map(p=>[p[0],p[1].toString()]),calls,m[Symbol.iterator]===m.entries];
    }"""),
    ("cross-realm", """() => {
        const f=document.createElement('iframe');document.body.appendChild(f);
        const child=f.contentWindow,p=new URLSearchParams('a=1');
        const i=child.URLSearchParams.prototype.entries.call(p),r=i.next();
        return [r.value,r.value instanceof child.Array, r.value instanceof Array,
            child.URLSearchParams.prototype[Symbol.iterator]===child.URLSearchParams.prototype.entries];
    }"""),
    ("style-snapshot", """() => {
        const e=document.createElement('div');e.setAttribute('style','color:red');
        const m=e.attributeStyleMap,i=m.entries();e.setAttribute('style','color:blue; width: 5px');
        return [Array.from(i).map(p=>[p[0],p[1].toString()]),Array.from(m).map(p=>[p[0],p[1].toString()])];
    }"""),
    ("wgsl", """() => {
        const f=navigator.gpu.wgslLanguageFeatures;
        return [f[Symbol.iterator]===f.values,Array.from(f.keys()).every(v=>typeof v==='string'),
            Array.from(f.entries()).every(p=>p[0]===p[1]),typeof f[0]];
    }"""),
]

EXPECTED = {
    'cross-realm-next': [[False,['a','1']], 'TypeError', 'TypeError'],
    'params': [['a','1'], [['c','3']], True, False, [['2','b',True,1],['3','c',True,1],['4','d',True,1]], True],
    'headers': [[['a','2'],['z','1']],['a','z'],['2','1'],[['2','a',True,1],['1','z',True,1]],True],
    'formdata': [[['a','1'],['a','2'],['b','3']],['a','a','b'],[['1','a',True],['2','a',True],['3','b',True]],True],
    'fonts': [True,['B'],True,True,[['B',True,True,1],['C',True,True,1],['A',True,True,1]],True,['B','C','A']],
    'nodes': [True,list(range(5)),list(range(5)),'undefined'],
    'style': [['color'],[['color','red']],[['color','red',True,1]],True],
    'cross-realm': [['a','1'],False,True,True],
    'style-snapshot': [[['color','red']],[['color','blue'],['width','5px']]],
    'wgsl': [True,True,True,'undefined'],
}

@pytest.mark.parametrize('name,script', CASES)
async def test_iteration_cases(name, script):
    browser=(await Browser(idl_path=IDL, real_network=False, stub_logging='off').__aenter__())
    try:
        page=browser.new_profile().new_page();(await page.set_content('<body></body>',url='https://iteration.test/', wait_until="commit"))
        result=page.evaluate('('+script+')()')
        assert result == EXPECTED[name]
    finally: (await browser.close())


async def test_headers_pair_semantics_and_fonts_set_semantics():
    browser=(await Browser(idl_path=IDL, real_network=False, stub_logging='off').__aenter__())
    try:
        page=browser.new_profile().new_page()
        assert page.evaluate("JSON.stringify(Array.from(new Headers([['z','1'],['a','2']]).keys()))") == '["a","z"]'
        assert page.evaluate("FontFaceSet.prototype[Symbol.iterator]===FontFaceSet.prototype.values") is True
    finally: (await browser.close())
