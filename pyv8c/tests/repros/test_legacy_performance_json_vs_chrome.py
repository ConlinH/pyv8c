"""Legacy navigation timing JSON must remain usable by performance collectors."""
import pytest

pytest.importorskip('pyv8c.pyv8')
from corpus import Repro
from harness import ReferenceUnavailable, chrome_reference, observe_here


SCRIPT = '''(() => {
  const out={};
  for(const name of ['timing','navigation']) {
    const value=performance[name];
    value.userField='ignored';
    const a=value.toJSON(),b=value.toJSON();
    out[name]={keys:Object.keys(a).sort(),plain:Object.getPrototypeOf(a)===Object.prototype,
      fresh:a!==b,custom:('userField' in a),
      numbers:Object.keys(a).every(k=>typeof a[k]==='number'&&Number.isFinite(a[k])),
      mirrors:Object.keys(a).every(k=>a[k]===value[k]),
      stringify:JSON.stringify(value)===JSON.stringify(b)};
    const field=name==='timing'?'navigationStart':'type';
    a[field]=-1;
    out[name].independent=b[field]===value[field]&&a[field]!==value[field];
  }
  return out;
})()'''


async def test_legacy_performance_json_matches_chrome():
    case=Repro('legacy-performance-json',SCRIPT,html='<!doctype html><title>timing</title>')
    try:
        async with chrome_reference() as reference:
            expected=(await reference.observe(case))
    except ReferenceUnavailable as exc:
        pytest.skip(str(exc))
    actual=(await observe_here(case))
    assert not expected.error
    assert actual==expected
    for value in actual.value.values():
        assert value['plain'] and value['fresh'] and value['numbers']
        assert value['mirrors'] and value['stringify'] and value['independent']
        assert not value['custom']
