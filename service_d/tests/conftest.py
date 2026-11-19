from pathlib import Path

import pytest

from d.config import Settings, load_static
from d.engine import Engine
from tests.fakes import FakeA, FakeB


def make_settings(tmp_path, b_url, **kw):
    base = dict(b_url=b_url, data_dir=str(tmp_path), a_notify_url="http://a/internal/order-events",
                catchup_quiet_ms=100, d_timeout_ms=1000, max_command_age_ms=1000, cancel_margin_ms=100,
                gate_deadline_ms=500, faults_enabled=True,
                symbols_file=str(Path(__file__).with_name("symbols_test.json")))  # lot 1 keeps quantities small
    base.update(kw)
    return Settings(**base)


@pytest.fixture
async def fake_b():
    b = FakeB()
    b.url = await b.start()
    yield b
    await b.stop()


@pytest.fixture
def fake_a():
    return FakeA()


@pytest.fixture
async def make_engine(tmp_path, fake_b, fake_a):
    engines = []

    async def make(**kw):
        s = make_settings(tmp_path, fake_b.url, **kw)
        e = Engine(s, load_static(s), a_transport=fake_a.transport())
        await e.start()
        engines.append(e)
        return e

    yield make
    for e in engines:
        try:
            await e.stop()
        except Exception:
            pass
