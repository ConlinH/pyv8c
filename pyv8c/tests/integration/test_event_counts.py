"""EventCounts observes host input without counting bubbling or synthetic input twice."""


async def test_event_counts_map_and_script_events(page):
    (await page.set_content('<!doctype html><body></body>', wait_until="commit"))
    assert page.evaluate('''(() => {
        const c=performance.eventCounts;
        const values=Array.from(c.values()),keys=Array.from(c.keys());
        let calls=0,valid=true;
        c.forEach(function(value,key,map){calls++;valid&&=map===c&&value===c.get(key)&&this===c},c);
        document.body.dispatchEvent(new MouseEvent('click',{bubbles:true}));
        document.body.click();
        return [c instanceof EventCounts,c===performance.eventCounts,c.size,
            new Set(keys).size,values.every(v=>v===0),c.get('click'),c.has('click'),
            c.get('mousemove')===undefined,c.has('mousemove'),typeof c.set,
            Array.from(c.entries()).length,Array.from(c).length,calls,valid];
    })()''') == [True, True, 36, 36, True, 0, True, True, False, 'undefined', 36, 36, 36, True]


async def test_event_counts_trusted_input_is_live_and_document_scoped(page):
    (await page.set_content('''<!doctype html><body style="margin:0">
        <button style="display:block;width:100px;height:40px">click</button>
        <iframe src="about:blank"></iframe></body>''', wait_until="commit"))
    page.evaluate('''globalThis.observed=[];
        document.querySelector('button').onclick=()=>observed.push(performance.eventCounts.get('click'));
        document.onclick=()=>observed.push(performance.eventCounts.get('click'));''')
    # Count the first host input even when eventCounts was never read before it.
    (await page.mouse.click(10, 10))
    assert page.evaluate('''[observed,performance.eventCounts.get('click'),
        performance.eventCounts.get('mousedown'),performance.eventCounts.get('pointerdown'),
        performance.eventCounts.has('mousemove'),
        document.querySelector('iframe').contentWindow.performance.eventCounts.get('click')]''') == [
            [1, 1], 1, 1, 1, False, 0]
    page.evaluate('globalThis.counts=performance.eventCounts')
    (await page.mouse.click(10, 10))
    assert page.evaluate("[counts===performance.eventCounts,counts.get('click')]") == [True, 2]
    (await page.set_content('<!doctype html><body>new document</body>', wait_until="commit"))
    assert page.evaluate("performance.eventCounts.get('click')") == 0


async def test_script_redispatch_clears_trust_without_counting_as_new_input(page):
    (await page.set_content('<!doctype html><body style="margin:0"><button style="display:block;width:100px;height:40px">click</button></body>', wait_until="commit"))
    page.evaluate('''globalThis.savedClick=null;globalThis.trust=[];
        document.querySelector('button').onclick=e=>{trust.push(e.isTrusted);if(!savedClick)savedClick=e};''')
    (await page.mouse.click(10, 10))
    assert page.evaluate("[savedClick.isTrusted,performance.eventCounts.get('click')]") == [True, 1]
    page.evaluate("document.querySelector('button').dispatchEvent(savedClick)")
    assert page.evaluate("[savedClick.isTrusted,trust,performance.eventCounts.get('click')]") == [False, [True, False], 1]


async def test_failed_reentrant_redispatch_does_not_change_the_original_trust(page):
    (await page.set_content('<!doctype html><body style="margin:0"><button style="display:block;width:100px;height:40px">click</button></body>', wait_until="commit"))
    page.evaluate('''globalThis.answer=[];document.querySelector('button').onclick=e=>{
        try{e.target.dispatchEvent(e)}catch(error){answer.push(error.name)}
        answer.push(e.isTrusted,performance.eventCounts.get('click'));
    };''')
    (await page.mouse.click(10, 10))
    assert page.evaluate('answer') == ['InvalidStateError', True, 1]


async def test_unused_mouse_enter_events_do_not_inflate_counts(page):
    (await page.set_content('<!doctype html><body style="margin:0"><button style="display:block;width:100px;height:40px">click</button></body>', wait_until="commit"))
    (await page.mouse.move(10, 10))
    assert page.evaluate("[performance.eventCounts.get('mouseenter'),performance.eventCounts.get('pointerenter')]") == [0, 4]


async def test_boundary_events_reach_capture_listeners_without_bubbling(page):
    (await page.set_content('<!doctype html><body style="margin:0"><button style="display:block;width:100px;height:40px">click</button></body>', wait_until="commit"))
    page.evaluate('''globalThis.boundary=[];
        for(const kind of ['mouseenter','pointerenter'])
            document.addEventListener(kind,e=>boundary.push([e.type,e.target.nodeName,e.composed,e.bubbles,e.eventPhase]),true);
    ''')
    (await page.mouse.move(10, 10))
    assert page.evaluate('boundary') == [
        [kind, node, False, False, 2 if node == '#document' else 1]
        for kind in ['pointerenter', 'mouseenter']
        for node in ['#document', 'HTML', 'BODY', 'BUTTON']
    ]


async def test_once_capture_listener_does_not_truncate_the_boundary_sequence(page):
    (await page.set_content('<!doctype html><body style="margin:0"><button style="display:block;width:100px;height:40px">click</button></body>', wait_until="commit"))
    page.evaluate('''globalThis.enters=[];document.addEventListener('mouseenter',
        e=>enters.push(e.target.nodeName),{capture:true,once:true});''')
    (await page.mouse.move(10, 10))
    assert page.evaluate("[enters,performance.eventCounts.get('mouseenter')]") == [['#document'], 4]


async def test_window_only_capture_does_not_enable_boundary_ancestor_walk(page):
    (await page.set_content('<!doctype html><body style="margin:0"><button style="display:block;width:100px;height:40px">click</button></body>', wait_until="commit"))
    page.evaluate('''globalThis.enters=[];for(const kind of ['mouseenter','pointerenter'])
        window.addEventListener(kind,e=>enters.push(e.type),true);''')
    (await page.mouse.move(10, 10))
    assert page.evaluate("[enters,performance.eventCounts.get('mouseenter'),performance.eventCounts.get('pointerenter')]") == [[], 0, 4]
