"""Materialization keeps graph identity and original exceptions across crossings."""

import pytest

from pyv8c import pyv8


@pytest.fixture
def context():
    owner = pyv8.Context(object())
    try:
        yield owner
    finally:
        owner.close()


@pytest.mark.parametrize("count", [0, 1, 15, 16, 17, 64, 1000])
def test_graph_identity_survives_small_and_large_materializations(context, count):
    handle = context.exec_js(f"""(() => {{
      const nodes=Array.from({{length:{count}}}, (_,value)=>({{value}}));
      const root={{nodes, again:nodes}};
      for(const node of nodes) {{node.root=root;node.shared=nodes[0];}}
      return root;
    }})()""")
    snapshot = handle.to_python()
    assert snapshot["nodes"] is snapshot["again"]
    for index, node in enumerate(snapshot["nodes"]):
        assert node["value"] == index
        assert node["root"] is snapshot
        assert node["shared"] is snapshot["nodes"][0]


@pytest.mark.parametrize(
    "source",
    [
        "({get value(){throw marker}})",
        "Object.defineProperty([], 0, {get(){throw marker}})",
        "Object.defineProperty(Array.from({length:32},()=>({})),31,{get(){throw marker}})",
        "new Proxy({}, {getOwnPropertyDescriptor(){throw marker}})",
    ],
)
def test_failed_snapshot_preserves_the_original_thrown_value(context, source):
    context.exec_js("globalThis.marker={message:'original failure'}")
    handle = context.exec_js(source)
    with pytest.raises(pyv8.JSException) as caught:
        handle.to_python()
    context.expose(thrown=caught.value.original)
    assert context.exec_js("thrown === marker") is True
    assert context.exec_js("1+1") == 2


def test_a_getter_can_reenter_and_collect_during_materialization(context):
    def collect():
        context.gc()
        return context.exec_js("shared")

    context.expose(collect=collect)
    handle = context.exec_js("""globalThis.shared={answer:42};
      ({first:shared, get second(){return collect()}, third:shared})""")
    snapshot = handle.to_python()
    assert snapshot["first"] is snapshot["second"] is snapshot["third"]
    assert snapshot["first"] == {"answer": 42}


def test_internal_key_array_reads_do_not_reach_array_prototype(context):
    handle = context.exec_js("""globalThis.probes=0;
      Object.defineProperty(Array.prototype, '0', {
        get(){probes++;throw Error('unexpected prototype getter')},
        configurable:true
      });
      ({first:1,second:2})""")
    try:
        assert handle.to_python() == {"first": 1, "second": 2}
        assert context.exec_js("probes") == 0
    finally:
        context.exec_js("delete Array.prototype[0]")
