async def test_id_changes_duplicates_and_moves(page):
    (await page.set_content('<body><div id="x" data-order="first"></div><div id="x" data-order="second"></div></body>', wait_until="commit"))
    assert page.evaluate('''(() => {
        let a=document.getElementById('x'), b=a.nextSibling;
        document.body.insertBefore(b,a);
        let first=document.getElementById('x')===b;
        b.id='y'; let renamed=document.getElementById('x')===a;
        a.removeAttribute('id'); let missing=document.getElementById('x')===null;
        a.setAttribute('id','x'); a.remove(); let detached=document.getElementById('x')===null;
        document.body.appendChild(a);
        return [first,renamed,missing,detached,document.getElementById('x')===a,
            document.getElementById('')===null,document.querySelector('#x')===a];
    })()''') == [True]*7


async def test_fragment_shadow_and_attribute_map_mutations(page):
    (await page.set_content('<body><div id="host"></div></body>', wait_until="commit"))
    assert page.evaluate('''(() => {
        let f=document.createDocumentFragment(), el=document.createElement('span');
        el.id='x'; f.appendChild(el);
        let isolated=f.getElementById('x')===el && document.getElementById('x')===null;
        document.body.appendChild(f);
        let moved=document.getElementById('x')===el && f.getElementById('x')===null;
        let root=document.getElementById('host').attachShadow({mode:'open'});
        root.innerHTML='<span id="x"></span>';
        return [isolated,moved,root.getElementById('x')!==el,
            root.querySelector('#x')!==el,document.getElementById('x')===el];
    })()''') == [True]*5
    def change():
        el=page.document.fn_getElementById('x')
        el.attributes.update(id='new')
        assert page.document.fn_getElementById('new') is el
        el.attributes.pop('id')
        assert page.document.fn_getElementById('new') is not el
        el.attributes.setdefault('id','next')
        assert page.document.fn_getElementById('next') is el
        el.attributes |= {'id':'union'}
        assert page.document.fn_getElementById('union') is el
        el.attributes = {'id':'replace'}
        assert page.document.fn_getElementById('replace') is el
        el.attributes.clear()
        assert page.document.fn_getElementById('replace') is not el
    page.realm.executor.call(change)


async def test_other_page_and_detached_tree_do_not_invalidate_snapshot(browser):
    profile=browser.new_profile(); a=profile.new_page(); b=profile.new_page()
    (await a.set_content('<body><div id="x"></div></body>', wait_until="commit")); (await b.set_content('<body></body>', wait_until="commit"))
    snapshot=a.realm.executor.call(a.document._descendant_snapshot)
    b.evaluate('document.body.appendChild(document.createElement("div"))')
    assert a.realm.executor.call(a.document._descendant_snapshot) is snapshot
    a.evaluate('let f=document.createDocumentFragment();f.appendChild(document.createElement("span"))')
    assert a.realm.executor.call(a.document._descendant_snapshot) is snapshot
    a.evaluate('document.body.textContent=""')
    assert a.realm.executor.call(a.document._descendant_snapshot) is not snapshot
    assert a.evaluate('document.getElementById("x")===null')


async def test_collection_prototype_names_do_not_scan_supported_names(page):
    (await page.set_content('<body><div id="length"></div><div id="item"></div></body>', wait_until="commit"))
    def verify():
        collection=page.document.fn_getElementsByTagName('div')
        def fail(): raise AssertionError('prototype member enumerated names')
        collection._supported_names=fail
        assert collection.__v8_name_get__('length') is None
        assert collection.__v8_name_query__('item') is None
        assert collection.__v8_name_set__('namedItem',1) is None
        assert collection.__v8_name_del__('toString') is None
    page.realm.executor.call(verify)


async def test_attribute_node_and_subtree_root_queries(page):
    (await page.set_content('<body><section id="root"><i id="x"></i></section></body>', wait_until="commit"))
    assert page.evaluate('''(() => {
        let root=document.getElementById('root'), el=document.getElementById('x');
        el.attributes.getNamedItem('id').value='y';
        let renamed=document.getElementById('x')===null && document.getElementById('y')===el;
        return [renamed,root.querySelector('#root')===null,root.querySelector('#y')===el];
    })()''') == [True]*3


async def test_removed_attribute_node_cannot_restore_an_id(page):
    (await page.set_content('<body><div id="x"></div></body>', wait_until="commit"))
    assert page.evaluate('''(() => {
        let el=document.getElementById('x'), a=el.attributes.getNamedItem('id');
        el.setAttribute('id','y');let updated=a.value==='y';
        let same=el.attributes.getNamedItem('id')===a;
        let removed=el.attributes.removeNamedItem('id');
        el.id='new';a.value='detached';
        return [updated,same,removed===a,a.ownerElement===null,el.id==='new',
            document.getElementById('detached')===null];
    })()''') == [True]*6


async def test_filtered_candidates_keep_tree_order_scopes_and_live_collections(page):
    await page.set_content('''<main><div><i id="other" class="hit"></i></div>
      <section><i id="same" class="hit"></i><i id="same"></i><b id="host"></b></section>
      </main>''', wait_until="commit")
    assert page.evaluate('''(() => {
        const main = document.querySelector('main'), section = main.querySelector('section');
        const earlier = main.querySelector('#other');
        const [a, b] = section.querySelectorAll('#same');
        const host = section.querySelector('#host');
        const shadowHost = document.createElement('div');
        host.appendChild(shadowHost);
        const shadow = shadowHost.attachShadow({mode:'open'});
        shadow.innerHTML = '<i id="same" class="hit"></i>';
        const original = document.querySelectorAll('main #same');
        const live = main.getElementsByTagName('i');
        const liveClass = main.getElementsByClassName('hit');
        let ok = original.length === 2 && original[0] === a && original[1] === b;
        ok &&= shadow.querySelectorAll('#same').length === 1 && live.length === 3;
        earlier.id = 'same';
        const renamed = main.querySelectorAll('#same, section i');
        ok &&= document.getElementById('same') === earlier
            && renamed.length === 3 && renamed[0] === earlier && renamed[1] === a && renamed[2] === b;
        a.attributes.getNamedItem('id').value = 'renamed';
        ok &&= main.querySelectorAll('section #same.hit').length === 0
            && shadow.querySelectorAll('i#same.hit').length === 1;
        b.className = 'hit';
        section.insertBefore(b, a);
        ok &&= live[1] === b && liveClass[1] === b && liveClass.length === 3
            && main.querySelector('section i#same.hit') === b;
        earlier.remove();
        ok &&= live.length === 2 && liveClass.length === 2 && document.getElementById('same') === b;
        return ok && original.length === 2 && original[0] === a && original[1] === b;
    })()''') is True


async def test_indexed_query_keeps_node_wrappers_and_prototype_descriptors(page):
    await page.set_content('<main><div id="item"></div><div id="item"></div></main>', wait_until="commit")
    assert page.evaluate('''(() => {
        const node = document.getElementById('item');
        const prototype = Object.getPrototypeOf(node);
        const names = Object.getOwnPropertyNames(prototype);
        const descriptors = Object.getOwnPropertyDescriptors(prototype);
        const query = Object.getOwnPropertyDescriptor(Element.prototype, 'querySelectorAll');
        const token = Symbol('test');
        Object.defineProperty(prototype, token, {value: node, configurable: true});
        const originalOwn = Object.getOwnPropertyNames(node);
        const original = document.querySelectorAll('main #item');
        node.id = 'renamed';
        const current = document.querySelectorAll('main #renamed')[0];
        const now = Object.getOwnPropertyDescriptor(Element.prototype, 'querySelectorAll');
        const sameDescriptor = (a, b) => Reflect.ownKeys(a).every(key => a[key] === b[key]);
        const intact = current === node && original[0] === node && current instanceof HTMLDivElement
            && Object.getPrototypeOf(current) === prototype && current[token] === node
            && JSON.stringify(Object.getOwnPropertyNames(current)) === JSON.stringify(originalOwn)
            && sameDescriptor(query, now)
            && JSON.stringify(Object.getOwnPropertyNames(prototype)) === JSON.stringify(names)
            && names.every(name => sameDescriptor(
                descriptors[name], Object.getOwnPropertyDescriptor(prototype, name)));
        delete prototype[token];
        return intact;
    })()''') is True


async def test_completed_id_index_updates_without_rebuilding_and_tag_query_skips_snapshot(page):
    await page.set_content('<body><div id="target"></div><div id="duplicate"></div></body>', wait_until="commit")

    def verify():
        doc = page.document
        doc._invalidate_tree()
        target, other = doc.query_selector_all('body div', doc)
        assert '_descendants_cache' not in doc.__dict__
        assert doc.query_selector_all('div#target', doc) == [target]
        index = doc.__dict__['_id_index']
        target.attributes['id'] = 'duplicate'
        assert doc.query_selector_all('div#duplicate', doc) == [target, other]
        target.attributes.pop('id')
        assert doc.query_selector_all('div#duplicate', doc) == [other]
        assert doc.__dict__['_id_index'] is index

    page.realm.executor.call(verify)


async def test_partial_id_index_handles_edits_before_and_after_its_cursor(page):
    await page.set_content('''<body><i></i><i id="cursor"></i><i id="later"></i></body>''',
                           wait_until="commit")

    def verify():
        doc = page.document
        doc._invalidate_tree()
        first, middle, last = doc.get_body()._children
        assert doc.fn_getElementById('cursor') is middle
        index = doc.__dict__['_id_index']
        first.attributes['id'] = 'cursor'
        last.attributes['id'] = 'cursor'
        assert doc.fn_getElementById('cursor') is first
        assert doc.query_selector_all('i#cursor', doc) == [first, middle, last]
        assert doc.__dict__['_id_index'] is index

    page.realm.executor.call(verify)


async def test_repeated_attribute_value_still_records_each_mutation(page):
    (await page.set_content('<body><div></div></body>', wait_until="commit"))
    assert page.evaluate('''(() => {
        let el=document.body.firstChild, o=new MutationObserver(()=>{});
        o.observe(el,{attributes:true,attributeOldValue:true});
        el.setAttribute('data-x','1');el.setAttribute('data-x','1');
        return JSON.stringify(o.takeRecords().map(r=>r.oldValue));
    })()''') == '[null,"1"]'


async def test_caches_release_removed_nodes_and_internal_mutations(page):
    (await page.set_content('<body><div id="x"></div></body>', wait_until="commit"))
    def verify():
        doc=page.document; doc.fn_getElementById('x'); doc._descendant_snapshot()
        body=doc.get_body()
        body._replace_children(())
        assert '_id_index' not in doc.__dict__
        assert '_descendants_cache' not in doc.__dict__
        assert doc.fn_getElementById('x') is not body
        root=doc.fn_createDocumentFragment()
        a=doc.fn_createElement('div'); a.set_id('x')
        b=doc.fn_createElement('div'); b.set_id('x')
        root.append_child(a); root.append_child(b)
        assert root.fn_getElementById('x') is a
        root._children.reverse()
        assert root.fn_getElementById('x') is b
        root._children[:] = [a]
        assert root.fn_getElementById('x') is a
    page.realm.executor.call(verify)


async def test_navigation_and_close_discard_document_query_caches(page):
    (await page.set_content('<body><div id="x"></div></body>', wait_until="commit"))
    old=page.document
    page.evaluate('document.getElementById("x");document.getElementsByTagName("div").length')
    (await page.set_content('<body><div id="y"></div></body>', wait_until="commit"))
    assert '_id_index' not in old.__dict__
    assert '_descendants_cache' not in old.__dict__
    page.evaluate('document.getElementById("y")')
    current=page.document
    (await page.close())
    assert '_id_index' not in current.__dict__
