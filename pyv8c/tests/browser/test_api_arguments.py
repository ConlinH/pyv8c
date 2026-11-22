"""Parameter semantics are owned by each API, including observable conversion effects."""

import json

import pytest

from pyv8c.async_api import Browser


@pytest.fixture
async def argument_page():
    browser = (await Browser(stub_logging="off", resource_loader=lambda url, request: {"url": url, "body": ""}).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://example.test/", wait_until="commit"))
        yield page
    finally:
        (await browser.close())


async def test_request_init_reads_members_in_chrome_order(argument_page):
    expected = ['body', 'cache', 'credentials', 'duplex',
                'headers', 'integrity', 'keepalive', 'method', 'mode', 'priority',
                'privateToken', 'redirect', 'referrer', 'referrerPolicy', 'signal',
                'targetAddressSpace']
    assert argument_page.evaluate('''(() => {
        const results = [];
        for (const url of ['https://example.test/', 'ftp:']) {
            const keys = [];
            const init = new Proxy({}, {get(target, name) {
                if (typeof name === 'string') keys.push(name);
                return undefined;
            }});
            try { new Request(url, init); } catch (_) {}
            results.push(keys);
        }
        return results;
    })()''') == [expected, expected]


async def test_request_init_stops_when_an_enum_conversion_throws(argument_page):
    assert argument_page.evaluate('''(() => {
        const keys = [];
        const init = new Proxy({}, {get(target, name) {
            if (typeof name === 'string') keys.push(name);
            return name === 'credentials' ? 'invalid' : undefined;
        }});
        try { new Request('https://example.test/', init); } catch (_) {}
        return keys;
    })()''') == ['body', 'cache', 'credentials']


async def test_input_event_init_reads_dictionary_members_in_chrome_order(argument_page):
    assert argument_page.evaluate('''(() => {
        const keys = [];
        const init = new Proxy({}, {get(target, name) {
            if (typeof name === 'string') keys.push(name);
            return undefined;
        }});
        new InputEvent('input', init);
        return keys;
    })()''') == ['bubbles', 'cancelable', 'composed', 'detail', 'sourceCapabilities',
                 'view', 'data', 'dataTransfer', 'inputType', 'isComposing', 'targetRanges']


async def test_input_event_init_validates_and_preserves_target_ranges(argument_page):
    assert argument_page.evaluate('''(() => {
        const range = new StaticRange({startContainer: document.body, startOffset: 0,
                                       endContainer: document.body, endOffset: 0});
        const event = new InputEvent('input', {targetRanges: [range]});
        const first = event.getTargetRanges(), second = event.getTargetRanges();
        const errors = [null, [{}]].map(targetRanges => {
            try { new InputEvent('input', {targetRanges}); return null; }
            catch (error) { return error.name; }
        });
        return [first.length, first[0] === range, first !== second, errors];
    })()''') == [1, True, True, ['TypeError', 'TypeError']]


@pytest.mark.parametrize(
    "expression",
    [
        "document.createElement('div').setAttribute('x', Symbol())",
        "localStorage.key(1n)",
        "new Event('x', 1)",
        "new Worker('worker.js', 1)",
        "new Worker('worker.js', {type:'invalid'})",
        "new Headers([['one']])",
        "new Headers().set('bad name', 'value')",
        "new Headers().set('name', 'bad\\nvalue')",
        "new Request('https://example.test', {method:'GET', body:'x'})",
        "new Request('https://example.test', {mode:'invalid'})",
        "new URL('https://example.test', null)",
    ],
)
async def test_api_type_errors(argument_page, expression):
    assert argument_page.evaluate(
        "(() => {try {" + expression + ";return false;}catch(error){return error instanceof TypeError;}})()"
    )


async def test_request_special_scheme_requires_host(argument_page):
    assert argument_page.evaluate("""(() => {
        try { new Request('ftp:', function () {}); }
        catch (error) {
            return [error.name, error.message];
        }
    })()""") == [
        "TypeError", "Failed to construct 'Request': Failed to parse URL from ftp:",
    ]
    assert argument_page.evaluate(
        "new Request('ftp://example.test/a', function () {}).url"
    ) == "ftp://example.test/a"


@pytest.mark.parametrize("thrown", ["new Error('sentinel')", "42", "null", "Symbol('sentinel')"])
async def test_conversion_exception_identity(argument_page, thrown):
    assert argument_page.evaluate(
        """(() => {
        const sentinel = """
        + thrown
        + """;
        try { document.createElement('div').setAttribute('id', {toString(){throw sentinel}}); }
        catch (error) { return error === sentinel; }
        return false;
    })()"""
    )


async def test_dictionary_uses_get_in_order_without_enumeration(argument_page):
    assert argument_page.evaluate("""(() => {
        const seen=[];
        const init=new Proxy({}, {
            ownKeys(){throw Error('must not enumerate')},
            has(){throw Error('must not probe presence')},
            get(target,key){seen.push(key);return undefined;}
        });
        new Event('x',init);
        return seen;
    })()""") == ["bubbles", "cancelable", "composed"]


async def test_ignored_extra_is_not_inspected(argument_page):
    assert argument_page.evaluate("""(() => {
        const {proxy,revoke}=Proxy.revocable({},{});revoke();
        return document.createElement('div').getAttribute('missing',proxy) === null;
    })()""")


async def test_sequence_converts_before_next_without_iterator_close(argument_page):
    assert argument_page.evaluate("""(() => {
        const seen=[];let index=0;
        const iterable={get [Symbol.iterator](){seen.push('iterator');return function(){return {
            next(){seen.push('next');return index++ === 0
                ? {value:{toString(){seen.push('convert');throw 42}},done:false}
                : {done:true};},
            return(){seen.push('close');return {done:true};}
        }}}};
        try{new Blob(iterable)}catch(error){if(error!==42)throw error}
        return seen;
    })()""") == ["iterator", "next", "convert"]


async def test_blob_sequence_error_has_the_constructor_context(argument_page):
    assert argument_page.evaluate("""(() => {
        try { new Blob(() => {}, () => {}); }
        catch (error) { return [error.name, error.message]; }
    })()""") == ["TypeError", "Failed to construct 'Blob': The object must have a "
                  "callable @@iterator property."]


async def test_a_blob_part_that_is_a_buffer_contributes_its_bytes(argument_page):
    """A ``BufferSource`` part is the bytes it holds, not the text it reads as.

    A buffer, a view of one and a ``DataView`` each contribute their bytes; a part that
    is none of those still goes through the string conversion it always had.
    """
    assert argument_page.evaluate("""(() => {
        const buffer = new Uint8Array([1, 2, 3]).buffer;
        return [new Blob([buffer]).size,
                new Blob([new Uint8Array(buffer, 1, 2)]).size,
                new Blob([buffer, 'ab']).size,
                new Blob([new DataView(buffer)]).size,
                new Blob([{a: 1}]).size];
    })()""") == [3, 2, 5, 3, 15]


async def test_timer_optional_and_variadic_arguments(argument_page):
    argument_page.evaluate("""globalThis.timerArgumentResults=[];
        clearTimeout();clearInterval();
        setTimeout((first,second)=>timerArgumentResults.push(first,second),0,42,null);
        setTimeout('timerArgumentResults.push(7)',0);
    """)
    (await argument_page.run_until_idle(timeout=1))
    assert json.loads(argument_page.evaluate("JSON.stringify(timerArgumentResults)")) == [42, None, 7]


async def test_domexception_constructor_converts_and_ignores_extra(argument_page):
    assert argument_page.evaluate("""(() => {
        const error=new DOMException(null,false,{toString(){throw 42}});
        const empty=new DOMException(undefined,undefined);
        return [error.message,error.name,error.code,empty.message,empty.name];
    })()""") == ["null", "false", 0, "", "Error"]


async def test_contains_invalid_tokens_is_a_lookup(argument_page):
    assert argument_page.evaluate("""(() => {
        const tokens=document.body.classList;
        return [tokens.contains(''),tokens.contains('a b')];
    })()""") == [False, False]


async def test_storage_unsigned_index_and_null_result(argument_page):
    assert argument_page.evaluate("""(() => {
        localStorage.clear();localStorage.setItem('only','value');
        return [localStorage.key(-1)===null,localStorage.key(2**32)==='only',localStorage.getItem('missing')===null];
    })()""") == [True, True, True]


async def test_event_dictionary_defaults_and_explicit_conversions(argument_page):
    assert argument_page.evaluate("""(() => {
        const defaults=new CloseEvent('close');
        const custom=new CloseEvent('close',{code:-1,reason:false,wasClean:[]});
        return [defaults.code,defaults.wasClean,custom.code,custom.reason,custom.wasClean,new MessageEvent('x',{origin:null}).origin];
    })()""") == [0, False, 65535, 'false', True, 'null']


async def test_websocket_validation_precedes_network_and_state(argument_page):
    assert argument_page.evaluate("""(() => {
        try{new WebSocket('ftp://example.test', ['duplicate','duplicate'])}
        catch(error){return error.name==='SyntaxError'}
        return false;
    })()""")
    from pyv8c.web.page_errors import DOMException
    from pyv8c.web.websocket import WebSocket

    socket=object.__new__(WebSocket)
    socket._state_value=WebSocket.OPEN
    socket._binary_type='blob'
    socket._ws=None
    socket.set_binaryType('invalid')
    assert socket._binary_type=='blob'
    with pytest.raises(DOMException):
        socket.fn_close(42)
    assert socket._state_value==WebSocket.OPEN


async def test_record_excludes_inherited_keys_and_converts_values(argument_page):
    assert argument_page.evaluate("""(() => {
        const source=Object.create({inherited:'skip'});source.key=null;
        return [String(new URLSearchParams(source)),new Headers(source).get('key')];
    })()""") == ["key=null", "null"]


async def test_dom_variadic_conversion_is_atomic(argument_page):
    assert argument_page.evaluate("""(() => {
        const node=document.createElement('div');node.textContent='old';
        try{node.replaceChildren('new',{toString(){throw 42}})}catch(error){}
        const preserved=node.textContent;
        node.replaceChildren(null,undefined,false);
        return [preserved,node.textContent];
    })()""") == ["old", "nullundefinedfalse"]


async def test_event_object_listener_and_handler_assignment(argument_page):
    assert argument_page.evaluate("""(() => {
        const target=new EventTarget();const listener={count:0,handleEvent(){this.count++}};
        target.addEventListener('x',listener,{once:true});
        target.dispatchEvent(new Event('x'));target.dispatchEvent(new Event('x'));
        document.body.onload=42;
        return [listener.count,document.body.onload===null];
    })()""") == [1, True]


async def test_promise_failures_reject_without_sync_throw(argument_page):
    argument_page.evaluate("""globalThis.argumentDone=false;(async()=>{
        const sentinel={marker:42};const result=[];
        try{await fetch({toString(){throw sentinel}})}catch(error){result.push(error===sentinel)}
        let promise;
        try{promise=new Response('bad-json').json();result.push(promise instanceof Promise)}
        catch(error){result.push(false)}
        try{await promise}catch(error){result.push(error instanceof SyntaxError)}
        const response=new Response('ok');await response.text();
        try{await response.text()}catch(error){result.push(error instanceof TypeError)}
        globalThis.argumentResult=result;globalThis.argumentDone=true;
    })()""")
    (await argument_page.run_until_idle(timeout=1))
    assert argument_page.evaluate("argumentDone")
    assert json.loads(argument_page.evaluate("JSON.stringify(argumentResult)")) == [True] * 4


async def test_worker_option_conversion_precedes_start(argument_page):
    assert argument_page.evaluate("""(() => {
        const seen=[];
        try{new Worker('not-fetched.js', {
            get type(){seen.push('type');throw 42},
            get name(){seen.push('name');return {toString(){seen.push('name-value');return 'worker'}}},
            get credentials(){seen.push('credentials');return 'omit'}
        })}catch(error){if(error!==42)throw error}
        return seen;
    })()""") == ["credentials", "name", "name-value", "type"]


async def test_idb_compound_key_path_and_transaction_scope(argument_page):
    argument_page.evaluate("""globalThis.idbArgumentResult=null;
        const request=indexedDB.open('argument-compound-path',1);
        request.onupgradeneeded=()=>{
            const store=request.result.createObjectStore('items',{keyPath:['group','id']});
            store.createIndex('byParts',['group','id']);
            store.add({group:'a',id:1});
        };
        request.onsuccess=()=>{
            const transaction=request.result.transaction(['items']);
            const query=transaction.objectStore('items').get(['a',1]);
            query.onsuccess=()=>{idbArgumentResult=query.result.id;request.result.close()};
        };
    """)
    (await argument_page.run_until_idle(timeout=1))
    assert argument_page.evaluate("idbArgumentResult") == 1
