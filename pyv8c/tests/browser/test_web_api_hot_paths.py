"""Hot queries and coercions observe current DOM state and preserve side effects."""

import gc
import json

import pytest

MARKUP = """<main id="outer"><section id="root">
  <div id="a" class="row"><span id="x" class="hit"></span>text<span id="y"></span></div>
  <div id="b" class="row"><span id="z" class="hit"></span></div>
</section></main>"""


@pytest.mark.parametrize(
    "selector, expected",
    [
        ("div.row > span.hit", ["x", "z"]),
        ("span.hit, div.row", ["a", "x", "b", "z"]),
        ("div.row > span.hit, span", ["x", "y", "z"]),
        ("main span.hit", ["x", "z"]),
        (".hit:not(#x)", ["z"]),
        ("div.row > span.hit + span", ["y"]),
        (".row ~ .row > span", ["z"]),
        ("div.row:nth-child(2) > span", ["z"]),
        ("section > div.row", ["a", "b"]),
    ],
)
async def test_group_matching_preserves_order_scope_and_combinators(page, selector, expected):
    (await page.set_content(MARKUP, wait_until="commit"))
    assert (
        page.evaluate(
            """(() => {
      const root=document.getElementById('root'), selector="""
            + json.dumps(selector)
            + """;
      return [Array.from(root.querySelectorAll(selector),node=>node.id),
              root.querySelector(selector)?.id ?? null];
    })()"""
        )
        == [expected, expected[0] if expected else None]
    )


async def test_repeated_group_queries_observe_attribute_and_tree_mutations(page):
    (await page.set_content(MARKUP, wait_until="commit"))
    assert page.evaluate("""(() => {
      const root=document.getElementById('root'), query=()=>root.querySelectorAll('div.row > span.hit');
      const ids=()=>Array.from(query(),node=>node.id), before=query();
      document.getElementById('x').className='';const changed=ids();
      const group=document.createElement('div');group.className='row';
      group.innerHTML='<span id="t" class="hit"></span>';root.appendChild(group);
      const added=ids();document.getElementById('b').remove();const removed=ids();
      root.insertBefore(group,root.firstChild);
      return [Array.from(before,node=>node.id),changed,added,removed,ids()];
    })()""") == [["x", "z"], ["z"], ["z", "t"], ["t"], ["t"]]


async def test_group_queries_keep_shadow_and_fragment_trees_separate(page):
    assert page.evaluate("""(() => {
      const host=document.createElement('div');document.body.appendChild(host);
      const shadow=host.attachShadow({mode:'open'});
      shadow.innerHTML='<div class="row"><span class="hit" id="shadow"></span></div>';
      const fragment=document.createDocumentFragment(), group=document.createElement('div');
      group.className='row';group.innerHTML='<span class="hit" id="fragment"></span>';
      fragment.appendChild(group);
      const query=root=>Array.from(root.querySelectorAll('div.row > span.hit'),node=>node.id);
      const before=[query(document),query(shadow),query(fragment)];
      document.body.appendChild(fragment);
      return [before,query(document),query(shadow),query(fragment)];
    })()""") == [[[], ["shadow"], ["fragment"]], ["fragment"], ["shadow"], []]


async def test_first_group_match_does_not_scan_later_candidates(page, monkeypatch):
    (await page.set_content(MARKUP, wait_until="commit"))

    def verify():
        from pyv8c.web.css.selectors import select

        root = page.document.fn_getElementById("root")
        first = page.document.fn_getElementById("x")

        def candidates():
            yield first
            pytest.fail("first-result query continued walking")

        def full_index():
            pytest.fail("first-result query indexed the whole tree")

        with monkeypatch.context() as patch:
            patch.setattr(root, "_descendants", candidates)
            patch.setattr(root, "_tag_index_snapshot", full_index)
            assert select(root, "div.row > span.hit", first=True) is first

    page.executor.call(verify)


async def test_attribute_hook_can_be_changed_and_removed_after_binding(page):
    (await page.set_content('<div id="target"></div>', wait_until="commit"))

    def verify():
        target = page.document.fn_getElementById("target")
        seen = []
        target._attribute_changed = lambda *args: seen.append(("first", args))
        target.fn_setAttribute("data-x", "one")
        target.fn_setAttribute("data-x", "one")
        target._attribute_changed = lambda *args: seen.append(("second", args))
        target.fn_setAttribute("data-x", "two")
        del target._attribute_changed
        target.fn_setAttribute("data-x", "three")
        assert seen == [("first", ("data-x", None, "one")), ("second", ("data-x", "one", "two"))]

    page.executor.call(verify)


async def test_string_conversion_keeps_subclasses_and_js_conversion_order(page):
    from pyv8c.foundation.arguments import dom_string

    from pyv8c import pyv8

    class Text(str):
        pass

    text = Text("unchanged")
    assert dom_string(text) is text
    assert page.evaluate("""(() => {
      const target=document.createElement('div'), seen=[];
      const key={[Symbol.toPrimitive](hint){seen.push('name:'+hint);return 'data-x'}};
      const value={[Symbol.toPrimitive](hint){seen.push('value:'+hint);return 'one'}};
      const observer=new MutationObserver(()=>{});
      observer.observe(target,{attributes:true,attributeOldValue:true});
      target.setAttribute(key,value);target.setAttribute('data-x','one');
      const records=observer.takeRecords();
      const marker={};let same=false;
      try{target.setAttribute({toString(){throw marker}},'two')}catch(error){same=error===marker}
      return [seen,records.length,records[0].oldValue,records[1].oldValue,
              target.getAttribute('data-x'),same];
    })()""") == [["name:string", "value:string"], 2, pyv8.Null, "one", "one", True]


async def test_repeated_matching_does_not_retain_work_until_cyclic_gc(page):
    (await page.set_content(MARKUP, wait_until="commit"))

    def verify():
        from pyv8c.web.css.selectors import matches, select

        root = page.document.fn_getElementById("root")
        node = page.document.fn_getElementById("x")
        selector = "div.row > span.hit"
        for _ in range(5):
            assert matches(node, selector)
            assert len(select(root, selector)) == 2
        gc.collect()
        enabled = gc.isenabled()
        gc.disable()
        try:
            before = len(gc.get_objects())
            for _ in range(500):
                assert matches(node, selector)
                assert len(select(root, selector)) == 2
            after = len(gc.get_objects())
            assert after - before < 40
        finally:
            if enabled:
                gc.enable()

    page.executor.call(verify)
