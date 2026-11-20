import pytest


@pytest.fixture
def anyio_backend():
    # The gateway targets asyncio (uvloop in containers); test on asyncio only.
    return "asyncio"
