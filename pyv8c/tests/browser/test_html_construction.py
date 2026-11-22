"""Single-pass token decoding preserves HTML recovery and insertion semantics."""

import pytest


@pytest.mark.parametrize("fragment", [False, True])
@pytest.mark.parametrize(
    "markup, expected",
    [
        (
            '<div id="a" title="a > b" data-x="a&amp;b">&lt;x&gt;</div>',
            '<div id="a" title="a &gt; b" data-x="a&amp;b">&lt;x&gt;</div>',
        ),
        ("<p>one<p>two", "<p>one</p><p>two</p>"),
        ("<i a=1 b=2></i><b a=3 b=4></b>", '<i a="1" b="2"></i><b a="3" b="4"></b>'),
        (
            '<div data-x=unquoted"quote>text</div><b>end</b>',
            '<div data-x="unquoted&quot;quote">text</div><b>end</b>',
        ),
    ],
)
async def test_attribute_decode_and_tolerant_boundaries(page, fragment, markup, expected):
    import json

    if fragment:
        page.evaluate("document.body.innerHTML=" + json.dumps(markup))
    else:
        (await page.set_content("<body>" + markup, wait_until="commit"))
    assert page.evaluate("document.body.innerHTML") == expected


async def test_tokens_are_retired_without_losing_document_write_insertion(page):
    (await page.set_content("""<body><i id=before></i><script>
      document.write('<b id=written title="a&amp;b">ok</b>');
    </script><i id=after></i></body>""", wait_until="commit"))
    assert page.evaluate("""[
      document.getElementById('written').getAttribute('title'),
      document.getElementById('written').nextSibling.id,
      document.getElementById('after').tagName]""") == ["a&b", "after", "I"]
    parser = page.navigation.parser
    assert parser.done and parser.tokens == []
    assert page.executor.call(parser.run) is True


async def test_node_insertion_hooks_remain_dynamic_and_reach_subtree_descendants(page):
    def verify():
        doc = page.document
        group = doc.fn_createElement("div")
        child = doc.fn_createElement("span")
        group.append_child(child)
        seen = []
        child._on_tree_inserted = lambda: seen.append("inserted")
        child._on_removed_from_document = lambda: seen.append("removed")
        doc.get_body().append_child(group)
        doc.get_body().fn_removeChild(group)
        assert seen == ["inserted", "removed"]
        del child._on_tree_inserted
        doc.get_body().append_child(group)
        assert seen == ["inserted", "removed"]

    page.executor.call(verify)
