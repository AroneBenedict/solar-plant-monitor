import pytest

from solarmon.db import connect, ingest, load_plants
from solarmon.pipeline import build
from solarmon.simulate import simulate


@pytest.fixture(scope="session")
def sim():
    return simulate(seed=1, days=45)


@pytest.fixture(scope="session")
def con(sim):
    c = connect()
    load_plants(c)
    ingest(c, sim.readings)
    return c


@pytest.fixture
def built():
    return build(":memory:", seed=3, days=30)
