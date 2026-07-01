"""Unit — the swappable session-store seam.

The one hard obligation for the session lifecycle in v3.0.0 is that the store be
**swappable**: v3.0.0 keeps sessions in RAM (re-handshake on reboot), and deep-sleep
support (persist to ESP32 RTC slow memory) becomes a clean v3.x drop-in *behind this same
interface* — not a rework. These tests pin the seam: a RAM store that round-trips sessions
by sid, and a custom store standing in for it through the identical get/put/drop contract.
"""
import pytest

from AlLoRa.Security.Session import Session
from AlLoRa.Security.Session_store import Session_store, RAM_session_store

KEY = bytes(16)
NONCE_PREFIX = bytes(8)


def _sess(sid):
    return Session(sid=sid, key=KEY, nonce_prefix=NONCE_PREFIX)


def test_ram_store_round_trips_a_session_by_sid():
    store = RAM_session_store()
    s = _sess(7)
    store.put(s)
    assert store.get(7) is s


def test_missing_sid_returns_none():
    assert RAM_session_store().get(99) is None


def test_drop_removes_the_session_and_is_idempotent():
    store = RAM_session_store()
    store.put(_sess(7))
    store.drop(7)
    assert store.get(7) is None
    store.drop(7)  # dropping an absent sid must not raise


def test_ram_store_is_a_session_store():
    assert isinstance(RAM_session_store(), Session_store)


def test_a_custom_store_can_replace_the_ram_store_behind_the_seam():
    # Stand-in for the future RTC-memory store: same interface, different backing.
    class Dict_store(Session_store):
        def __init__(self):
            self._d = {}
        def get(self, sid):
            return self._d.get(sid)
        def put(self, session):
            self._d[session.sid] = session
        def drop(self, sid):
            self._d.pop(sid, None)

    store = Dict_store()
    s = _sess(3)
    store.put(s)
    assert store.get(3) is s
    store.drop(3)
    assert store.get(3) is None


def test_base_store_defines_the_seam_but_is_not_usable_directly():
    with pytest.raises(NotImplementedError):
        Session_store().get(1)
