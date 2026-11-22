"""Resources shared by the local behaviour corpus."""
import pytest


@pytest.fixture(scope="session", autouse=True)
async def corpus_engine_lifetime():
    yield
    from harness import close_engine
    (await close_engine())
