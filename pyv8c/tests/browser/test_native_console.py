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


def test_console_memory_matches_chrome_shape(page):
    assert page.evaluate('''(() => {
      const memory = console.memory;
      const keys = ['jsHeapSizeLimit', 'totalJSHeapSize', 'usedJSHeapSize'];
      const proto = Object.getPrototypeOf(memory);
      return typeof memory === 'object' && memory !== null &&
             Object.keys(memory).length === 0 &&
             keys.every(key => typeof memory[key] === 'number' && memory[key] > 0) &&
             String(memory) === '[object MemoryInfo]' &&
             proto[Symbol.toStringTag] === 'MemoryInfo' &&
             !Object.prototype.hasOwnProperty.call(proto, 'constructor') &&
             keys.every(key => {
               const d = Object.getOwnPropertyDescriptor(proto, key);
               return d && typeof d.get === 'function' && d.set === undefined;
             });
    })()''') is True
