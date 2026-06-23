"""ConnectionManager: survive a broken viewer, prune it, and scope events per env."""
from __future__ import annotations

from odin.api.ws import ConnectionManager


class FakeWS:
    def __init__(self, dead: bool = False):
        self.sent: list = []
        self.dead = dead

    async def accept(self):
        pass

    async def send_json(self, message):
        if self.dead:
            raise RuntimeError("broken pipe")
        self.sent.append(message)


async def test_broadcast_delivers_persists_and_prunes_dead(tmp_path):
    mgr = ConnectionManager(tmp_path)
    good, bad = FakeWS(), FakeWS(dead=True)
    await mgr.connect(good)
    await mgr.connect(bad)

    await mgr.broadcast({"type": "a", "env": "default"})   # bad raises, must not propagate
    assert good.sent == [{"type": "a", "env": "default"}]   # live viewer got it
    assert len(mgr.get_events("default")) == 1              # persisted regardless of viewers

    await mgr.broadcast({"type": "b", "env": "default"})    # bad was pruned -> not retried
    assert good.sent[-1] == {"type": "b", "env": "default"}


async def test_disconnect_is_idempotent(tmp_path):
    mgr = ConnectionManager(tmp_path)
    w = FakeWS()
    await mgr.connect(w)
    mgr.disconnect(w)
    mgr.disconnect(w)                                       # second time must not raise


async def test_events_are_scoped_per_env(tmp_path):
    mgr = ConnectionManager(tmp_path)
    await mgr.broadcast({"type": "x", "env": "staging"})
    await mgr.broadcast({"type": "y", "env": "prod"})
    assert mgr.get_events("staging") == [{"type": "x", "env": "staging"}]
    assert mgr.get_events("prod") == [{"type": "y", "env": "prod"}]
    assert mgr.get_events("default") == []                  # an untouched env is empty
