"""Promise WebIDL operations must reject bad receivers without aborting JS."""
import pytest


@pytest.mark.parametrize('interface,method,real_expression', [
    ('Keyboard', 'getLayoutMap', 'navigator.keyboard'),
    ('Keyboard', 'lock', 'navigator.keyboard'),
    ('MediaCapabilities', 'decodingInfo', 'navigator.mediaCapabilities'),
    ('MediaCapabilities', 'encodingInfo', 'navigator.mediaCapabilities'),
    ('Navigator', 'getBattery', 'navigator'),
    ('NavigatorUAData', 'getHighEntropyValues', 'navigator.userAgentData'),
    ('Permissions', 'query', 'navigator.permissions'),
    ('RTCPeerConnection', 'createOffer', 'new RTCPeerConnection()'),
    ('RTCPeerConnection', 'createAnswer', 'new RTCPeerConnection()'),
    ('RTCPeerConnection', 'setLocalDescription', 'new RTCPeerConnection()'),
])
async def test_promise_operation_rejects_invalid_receiver(page, interface, method, real_expression):
    (await page.set_content('<!doctype html><body></body>', url='https://fixture.test/', wait_until="commit"))
    page.evaluate(f"""(() => {{
        globalThis.results=[];
        const operation={interface}.prototype.{method};
        const real={real_expression};
        for (const receiver of [window, {{}}, null, {interface}.prototype]) {{
            for (const args of [[], [real, 'extra']]) {{
                const row={{}}; results.push(row);
                const result=operation.apply(receiver,args);
                row.promise=result instanceof Promise;
                result.then(() => row.fulfilled=true, error => {{
                    row.error=error.name; row.message=error.message;
                }});
            }}
        }}
    }})()""")
    (await page.wait_for('results.length===8 && results.every(row=>row.error)', timeout=2))
    assert page.evaluate('results') == [{
        'promise': True, 'error': 'TypeError',
        'message': f"Failed to execute '{method}' on '{interface}': Illegal invocation",
    }] * 8


async def test_keyboard_valid_receiver_still_fulfills(page):
    (await page.set_content('<!doctype html><body></body>', url='https://fixture.test/', wait_until="commit"))
    page.evaluate("""globalThis.results=[];
        navigator.keyboard.getLayoutMap().then(map=>results.push(map instanceof KeyboardLayoutMap));
        navigator.keyboard.lock().then(()=>results.push(true));
    """)
    (await page.wait_for('results.length===2', timeout=2))
    assert page.evaluate('results') == [True, True]


async def test_permissions_argument_errors_reject_and_preserve_the_getter_exception(page):
    (await page.set_content('<!doctype html><body></body>', url='https://fixture.test/', wait_until="commit"))
    page.evaluate('''globalThis.permissionResults=[];
        const error=new RangeError('descriptor');
        for(const fn of [()=>navigator.permissions.query(),
            ()=>navigator.permissions.query(null),
            ()=>navigator.permissions.query({get name(){throw error}})]) {
            const row={};permissionResults.push(row);
            const promise=fn();row.promise=promise instanceof Promise;
            promise.catch(e=>{row.name=e.name;row.message=e.message;row.original=e===error});
        }''')
    (await page.wait_for('permissionResults.every(row=>row.name)', timeout=2))
    assert page.evaluate('permissionResults') == [
        {'promise': True, 'name': 'TypeError', 'message': "Failed to execute 'query' on 'Permissions': 1 argument required, but only 0 present.", 'original': False},
        {'promise': True, 'name': 'TypeError', 'message': "Failed to execute 'query' on 'Permissions': parameter 1 is not of type 'object'.", 'original': False},
        {'promise': True, 'name': 'RangeError', 'message': 'descriptor', 'original': True},
    ]


@pytest.mark.parametrize('child_function', [False, True])
async def test_permissions_borrowed_operation_uses_its_function_realm(page, child_function):
    (await page.set_content('<!doctype html><body><iframe></iframe></body>', url='https://fixture.test/', wait_until="commit"))
    page.evaluate('''globalThis.answers=[];
        const child=document.querySelector('iframe').contentWindow;
        const operation=CHILD?child.Permissions.prototype.query:Permissions.prototype.query;
        const receiver=CHILD?navigator.permissions:child.navigator.permissions;
        for(const args of [[],[{}],[{name:'not-a-permission'}],[{name:'push'}]]){
            const row={};answers.push(row);
            const promise=operation.apply(receiver,args);
            row.parentPromise=promise instanceof Promise;row.childPromise=promise instanceof child.Promise;
            promise.catch(error=>{row.error=error.name;row.parentError=error instanceof TypeError||error instanceof DOMException;
                row.childError=error instanceof child.TypeError||error instanceof child.DOMException});
        }'''.replace('CHILD', str(child_function).lower()))
    (await page.wait_for('answers.length===4&&answers.every(row=>row.error)', timeout=2))
    assert page.evaluate('answers') == [{
        'parentPromise': not child_function, 'childPromise': child_function,
        'error': error, 'parentError': not child_function, 'childError': child_function,
    } for error in ['TypeError', 'TypeError', 'TypeError', 'NotSupportedError']]


@pytest.mark.parametrize('child_function', [False, True])
async def test_permissions_success_uses_the_receiver_realm(page, child_function):
    (await page.set_content('<!doctype html><body><iframe></iframe></body>', url='https://fixture.test/', wait_until="commit"))
    page.evaluate('''globalThis.answer=null;
        const child=document.querySelector('iframe').contentWindow;
        const operation=CHILD?child.Permissions.prototype.query:Permissions.prototype.query;
        const receiver=CHILD?navigator.permissions:child.navigator.permissions;
        const promise=operation.call(receiver,{name:'geolocation'});
        promise.then(status=>answer=[promise instanceof Promise,promise instanceof child.Promise,
            status instanceof PermissionStatus,status instanceof child.PermissionStatus]);
        '''.replace('CHILD', str(child_function).lower()))
    (await page.wait_for('answer!==null', timeout=2))
    assert page.evaluate('answer') == [child_function, not child_function] * 2
