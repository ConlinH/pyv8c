"""Native crossings measured without page scheduling or network work."""

from __future__ import annotations

import json

from benchmarks.cases.execution import measure_operations

CASE_NAMES = (
    "callback",
    "method",
    "getter_number",
    "getter_text",
    "setter",
    "object_argument",
    "string_argument",
    "python_call",
    "exec_cached",
    "snapshot",
    "snapshot_graph",
    "snapshot_large",
)
CROSSINGS = 2000
TEXT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/156.0.0.0 Safari/537.36"


async def run_case(name, iterations=60):
    from pyv8c import pyv8

    if name not in CASE_NAMES or iterations < 1:
        raise ValueError("unknown workload or nonpositive operation count")

    class Host:
        __v8_idl_spec__ = pyv8.IdlSpec(
            methods=(pyv8.MethodSpec(name="method", callback="method"),),
            attributes=(
                pyv8.AttributeSpec(name="number", getter="number"),
                pyv8.AttributeSpec(name="text", getter="text"),
                pyv8.AttributeSpec(name="value", getter="get_value", setter="set_value"),
            ),
        )

        def __init__(self):
            self.calls = 0
            self.value = None

        def method(self, value):
            self.calls += 1
            return value

        def number(self):
            self.calls += 1
            return 42

        def text(self):
            self.calls += 1
            return TEXT

        def set_value(self, value):
            self.calls += 1
            self.value = value

        def get_value(self):
            return self.value

    host = Host()
    context = pyv8.Context(host)
    handle = None
    try:
        context.expose(callback=host.method, echo=lambda value: value)
        count = CROSSINGS
        checksum = sum(range(count))
        source, expected = {
            "callback": ("sum += callback(i)", checksum),
            "method": ("sum += method(i)", checksum),
            "getter_number": ("sum += number", 42 * count),
            "getter_text": (
                "sum += text.charCodeAt(i % " + str(len(TEXT)) + ")",
                sum(ord(TEXT[i % len(TEXT)]) for i in range(count)),
            ),
            "setter": ("value = i; sum += i", checksum),
            "object_argument": ("sum += echo(payload) === payload ? 1 : 0", count),
            "string_argument": (
                "sum += echo(message).charCodeAt(i % " + str(len(TEXT)) + ")",
                sum(ord(TEXT[i % len(TEXT)]) for i in range(count)),
            ),
        }.get(name, ("", None))
        scale = 1
        if name == "exec_cached":
            script = pyv8.Script(context, "21*2")
            operation = lambda: context.exec_js(script)
            expected = 42
        elif name == "python_call":
            handle = context.exec_js("(value) => value.length")
            operation = lambda: handle(TEXT)
            expected = len(TEXT)
        elif name in ("snapshot", "snapshot_graph", "snapshot_large"):
            expression = (
                "({first:1, second:'two', third:true, fourth:null})"
                if name == "snapshot"
                else "(() => {const child={value:42}; return {a:child,b:child,items:[1,2,3]}})()"
            )
            if name == "snapshot_large":
                expression = """(() => {
                    const items=Array.from({length:128},(_,value)=>({value}));
                    return {items,alias:items[0]};
                })()"""
            handle = context.exec_js(expression)
            operation = handle.to_python
            expected = (
                {"first": 1, "second": "two", "third": True, "fourth": pyv8.Null}
                if name == "snapshot"
                else {"a": {"value": 42}, "b": {"value": 42}, "items": [1, 2, 3]}
            )
            if name == "snapshot_large":
                expected = {"items": [{"value": i} for i in range(128)], "alias": {"value": 0}}
        else:
            script = pyv8.Script(
                context,
                "(() => {let sum=0;const payload={a:1};"
                f"const message={json.dumps(TEXT)};"
                f"for(let i=0;i<{count};i++){{{source};}} return sum;}})()",
            )
            operation = lambda: context.exec_js(script)
            scale = count
        batched = scale > 1 or name == "snapshot_large"
        warmup = 5 if batched else 200
        operations = iterations if batched else iterations * 200
        for _ in range(warmup):
            assert (operation()) == expected
        result = (measure_operations(operation, expected, operations))
        if name in ("callback", "method", "getter_number", "getter_text", "setter"):
            assert host.calls == (iterations + 5) * count
        if name == "setter":
            assert host.value == count - 1
        if name == "snapshot_graph":
            snapshot = (operation())
            assert snapshot["a"] is snapshot["b"]
        if name == "snapshot_large":
            snapshot = (operation())
            assert snapshot["alias"] is snapshot["items"][0]
        # Latency samples describe entire batches; throughput counts actual crossings.
        result["median"] /= scale
        result["min"] /= scale
        result.update(
            unit="us/crossing",
            detail=f"native {name}; {operations} batches of {scale}; GC=default",
            workload=f"bridge-{name}-v1:{operations}:{scale}",
            crossings_per_operation=scale,
            warmup=warmup,
        )
        return result
    finally:
        context.close()
