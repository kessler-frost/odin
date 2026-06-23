"""ConnectionManager: broadcast must survive a broken viewer and prune it."""
from __future__ import annotations

import odin.api.ws as wsmod
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


def _manager(tmp_path, monkeypatch) -> ConnectionManager:
    monkeypatch.setattr(wsmod, "EVENTS_LOG", tmp_path / "events.jsonl")
    return ConnectionManager()


async def test_broadcast_delivers_persists_and_prunes_dead(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch)
    good, bad = FakeWS(), FakeWS(dead=True)
    await mgr.connect(good)
    await mgr.connect(bad)

    await mgr.broadcast({"type": "a"})          # bad raises, must not propagate
    assert good.sent == [{"type": "a"}]         # live viewer got it
    assert len(mgr.get_events()) == 1           # persisted regardless of viewers

    await mgr.broadcast({"type": "b"})          # bad was pruned -> not retried
    assert good.sent == [{"type": "a"}, {"type": "b"}]


async def test_disconnect_is_idempotent(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch)
    w = FakeWS()
    await mgr.connect(w)
    mgr.disconnect(w)
    mgr.disconnect(w)                           # second time must not raise


async def test_broadcast_with_no_viewers_still_persists(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch)
    await mgr.broadcast({"type": "x"})
    assert mgr.get_events() == [{"type": "x"}]
