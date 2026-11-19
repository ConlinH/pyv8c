"""Browser console conversions run with no remote debugger connected."""
import pytest

pytest.importorskip('pyv8c.pyv8')


def test_console_count_converts_label_but_object_log_does_not_stringify(page):
    assert page.evaluate('''(() => {
      let calls=0;const label={toString(){calls++;return 'label'}};
      console.count(label);console.countReset(label);const countCalls=calls;
      console.log(label);console.debug('%o',label);
      return [countCalls,calls];
    })()''') == [2,2]


def test_console_does_not_eagerly_read_object_accessors(page):
    assert page.evaluate('''(() => {
      let reads=0;const object={get id(){reads++;return 'x'},get length(){reads++;return 1}};
      console.log(object);console.dir(object);console.table(object);return reads;
    })()''') == 0
