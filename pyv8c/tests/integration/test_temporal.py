"""The native V8 build ships Temporal, as the reference browser does."""


async def test_temporal_is_a_shipped_global(page):
    assert page.evaluate("typeof Temporal") == "object"
    assert page.evaluate("Temporal.PlainDate.from('2024-01-01').toString()") == "2024-01-01"
    assert "Temporal" in page.evaluate("Object.getOwnPropertyNames(window)")
