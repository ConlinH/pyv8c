"""WebAssembly modules keep their compiled code across structured clones."""
from pyv8c.async_api import Browser


async def test_structured_clone_module_and_shared_references():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        result = page.evaluate("""(() => {
          const bytes = new Uint8Array([0,97,115,109,1,0,0,0]);
          const module = new WebAssembly.Module(bytes);
          const copy = structuredClone({first:module, second:module});
          return [copy.first !== module, copy.first === copy.second,
                  copy.first instanceof WebAssembly.Module,
                  new WebAssembly.Instance(copy.first) instanceof WebAssembly.Instance];
        })()""")
        assert result == [True, True, True, True]
    finally:
        (await browser.close())


async def test_module_can_cross_worker_isolates():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result=null;
          let script = `onmessage = e => { let m=e.data;
            postMessage([m instanceof WebAssembly.Module,
              new WebAssembly.Instance(m) instanceof WebAssembly.Instance]); }`;
          let worker = new Worker(URL.createObjectURL(new Blob([script],{type:'text/javascript'})));
          worker.onmessage = e => {result=e.data};
          worker.postMessage(new WebAssembly.Module(new Uint8Array([0,97,115,109,1,0,0,0])));""")
        (await page.wait_for('result !== null', timeout=5))
        assert page.evaluate('result') == [True, True]
    finally:
        (await browser.close())


async def test_worker_can_post_compiled_module_to_page():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("""window.result=null;
          let script = `postMessage(new WebAssembly.Module(new Uint8Array([0,97,115,109,1,0,0,0])))`;
          let worker = new Worker(URL.createObjectURL(new Blob([script],{type:'text/javascript'})));
          worker.onmessage = e => {let m=e.data; result=[m instanceof WebAssembly.Module,
            new WebAssembly.Instance(m) instanceof WebAssembly.Instance]};""")
        (await page.wait_for('result !== null', timeout=5))
        assert page.evaluate('result') == [True, True]
    finally:
        (await browser.close())


async def test_structured_clone_memory_errors_include_chrome_context():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        result = page.evaluate("""(() => {
          let values = [new WebAssembly.Memory({initial:1}),
                        new WebAssembly.Memory({initial:1,maximum:1,shared:true})];
          return values.map(value => {try {structuredClone(value)}
            catch (e) {return [e.name,e.message]}});
        })()""")
        assert result == [
            ['DataCloneError', "Failed to execute 'structuredClone' on 'Window': #<Memory> could not be cloned."],
            ['DataCloneError', "Failed to execute 'structuredClone' on 'Window': SharedArrayBuffer transfer requires self.crossOriginIsolated."],
        ]
    finally:
        (await browser.close())
