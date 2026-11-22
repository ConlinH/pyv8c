async def test_transfer_validation_precedes_getters_and_preserves_other_ports(page):
    assert page.evaluate('''(()=>{
      const b=new ArrayBuffer(8),channel=new MessageChannel();let reads=0,error;
      const value={get x(){reads++;return 1}};
      try{structuredClone(value,{transfer:[b,channel.port1,b]})}catch(e){error=e.name}
      return [error,reads,b.byteLength];
    })()''') == ['DataCloneError',0,8]


async def test_transfer_does_not_call_page_array_buffer_hooks(page):
    assert page.evaluate('''(()=>{
      let calls=0;const b=new ArrayBuffer(4);
      b.transfer=()=>{calls++;throw 1};
      Object.defineProperty(b,'detached',{get(){calls++;throw 2}});
      const c=structuredClone(b,{transfer:[b]});
      return [calls,c.byteLength,Object.getOwnPropertyDescriptor(ArrayBuffer.prototype,'byteLength').get.call(b)];
    })()''') == [0,4,0]

async def test_clone_port_prepares_result_before_moving_ownership(page):
    assert page.evaluate("""(()=>{
      const channel=new MessageChannel(), b=new ArrayBuffer(4); let reads=0;
      try { structuredClone({port:channel.port1,get bad(){reads++;throw new Error('stop')}},
                             {transfer:[b,channel.port1]}) } catch(e) {}
      const clone=structuredClone({port:channel.port1},{transfer:[channel.port1]});
      clone.port.onmessage=e=>globalThis.portResult=e.data;
      channel.port2.postMessage('ok');
      return [reads,b.byteLength,clone.port instanceof MessagePort,clone.port!==channel.port1];
    })()""") == [1,4,True,True]
    (await page.wait_for("globalThis.portResult==='ok'", timeout=2))


async def test_clone_classification_does_not_read_constructor(page):
    assert page.evaluate("""(()=>{
      let reads=0;const v={a:1};Object.defineProperty(v,'constructor',{get(){reads++;throw 1}});
      return [structuredClone(v).a,reads];
    })()""") == [1,0]
