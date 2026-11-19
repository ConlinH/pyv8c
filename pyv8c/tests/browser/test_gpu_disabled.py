"""A disabled GPU consistently denies contexts and adapters in pages and Workers."""
import pytest

pytest.importorskip('pyv8c.pyv8')
from pyv8c import Browser


def test_disabling_gpu_keeps_cpu_canvas_and_native_interfaces():
    browser=Browser(gpu_enabled=False,stub_logging='off')
    try:
        page=browser.new_profile().new_page();page.set_content('<body></body>',url='https://gpu.test/')
        assert page.evaluate('''(() => {
          const c=document.createElement('canvas'),o=new OffscreenCanvas(2,2);
          return [c.getContext('webgl')===null,c.getContext('webgl2')===null,
                  c.getContext('webgpu')===null,c.getContext('2d') instanceof CanvasRenderingContext2D,
                  o.getContext('webgl')===null,o.getContext('2d') instanceof OffscreenCanvasRenderingContext2D,
                  typeof WebGLRenderingContext,typeof navigator.gpu.requestAdapter];
        })()''') == [True]*6+['function','function']
        page.evaluate('window.answer=1;navigator.gpu.requestAdapter().then(a=>answer=a===null)')
        page.wait_for('answer!==1',timeout=3)
        assert page.evaluate('answer') is True
    finally:browser.close()


def test_worker_inherits_disabled_gpu():
    browser=Browser(gpu_enabled=False,stub_logging='off')
    try:
        page=browser.new_profile().new_page();page.set_content('<body></body>',url='https://gpu.test/')
        page.evaluate('''window.answer=null;
          const worker=new Worker(URL.createObjectURL(new Blob([
            `Promise.resolve(navigator.gpu.requestAdapter()).then(a=>{
              const c=new OffscreenCanvas(1,1);
              postMessage([a===null,c.getContext('webgl')===null,!!c.getContext('2d')]);
            });`
          ],{type:'text/javascript'})));
          worker.onmessage=e=>{answer=e.data;worker.terminate()};''')
        page.wait_for('answer!==null',timeout=3)
        assert page.evaluate('answer') == [True]*3
    finally:browser.close()


def test_gpu_option_requires_a_boolean():
    with pytest.raises(TypeError,match='gpu_enabled'):
        Browser(gpu_enabled='false')
