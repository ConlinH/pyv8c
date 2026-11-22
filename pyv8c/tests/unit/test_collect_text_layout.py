"""The reference probe must use the same IDL default as a normal browser."""
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("idl", [None, "custom.zip"])
def test_comparison_uses_packaged_or_explicit_idl(monkeypatch, tmp_path, idl):
    pytest.importorskip("pyv8c.pyv8", reason="the reference collector imports browser bindings")
    path = Path(__file__).resolve().parents[3] / "tools" / "collect_text_layout.py"
    spec = importlib.util.spec_from_file_location("collect_text_layout_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "collect", lambda *a, **kw: {"browser": "test", "cases": {}})
    received = []
    async def run_here(cases, idl_path):
        received.append(idl_path)
        return {"cases": {}}
    monkeypatch.setattr(module, "_run_here", run_here)
    arguments = ["--compare", "--output", str(tmp_path / "reference.json")]
    if idl is not None:
        arguments += ["--idl", idl]
    assert module.main(arguments) == 0
    assert received == [None if idl is None else Path(idl)]
