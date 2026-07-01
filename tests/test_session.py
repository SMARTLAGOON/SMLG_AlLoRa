"""Unit — the secure-session model (sid + key + counter), crypto-agnostic.

A Session is the per-peer secure-session state created by the handshake (increment 2
step 3) and consumed by the secure transfer (step 4). It is deliberately crypto-agnostic:
it holds the AES key and the per-session nonce prefix as *opaque bytes* it never
interprets — the swappable crypto backend reads them — and owns the two pieces of
stateful bookkeeping the wire depends on:

  * a **monotonic send counter** for the 2-byte anti-replay field, which never wraps
    inside a session (it signals exhaustion so the node re-handshakes with a fresh key
    and nonce prefix — never reusing a (key, nonce) pair);
  * the **receive-side anti-replay window** (tested via test_replay_window; exercised
    end-to-end here).

The exact nonce/AAD byte layout is intentionally *not* fixed here — that is deferred to a
crypto-review pass, since the wire bytes are a published surface not worth freezing early.
The Session only guarantees a unique, monotone counter and opaque custody of the key
material.
"""
import pytest

from AlLoRa.Security.Session import Session, SessionExhausted


KEY = bytes(range(16))              # opaque 16-byte AES key stand-in
SEND_PREFIX = bytes(range(8))       # opaque per-direction nonce prefixes (send / receive)
RECV_PREFIX = bytes(range(8, 16))


def _session(sid=7):
    return Session(sid=sid, key=KEY, send_nonce_prefix=SEND_PREFIX, recv_nonce_prefix=RECV_PREFIX)


def test_session_holds_its_identity_and_key_material_opaquely():
    s = _session(sid=42)
    assert s.sid == 42
    assert s.key == KEY
    assert s.send_nonce_prefix == SEND_PREFIX
    assert s.recv_nonce_prefix == RECV_PREFIX


def test_send_counter_is_strictly_monotonic_starting_at_one():
    s = _session()
    assert s.next_counter() == 1
    assert s.next_counter() == 2
    assert s.next_counter() == 3


def test_send_counter_signals_exhaustion_instead_of_wrapping():
    s = _session()
    # Drain the whole 2-byte counter space (1..MAX). None of these may raise.
    for expected in range(1, Session.MAX_COUNTER + 1):
        assert s.next_counter() == expected
    assert s.counter_exhausted() is True
    # The next frame would reuse a counter (=> a reused nonce): refuse it.
    with pytest.raises(SessionExhausted):
        s.next_counter()


def test_counter_is_not_exhausted_before_the_space_is_used_up():
    s = _session()
    s.next_counter()
    assert s.counter_exhausted() is False


def test_accept_rejects_a_replayed_receive_counter():
    # accept() is the receive side: it delegates to the session's anti-replay window.
    s = _session()
    assert s.accept(1) is True
    assert s.accept(2) is True
    assert s.accept(1) is False   # replay of an already-seen frame


def test_send_and_receive_counter_spaces_are_independent():
    # The counter this node *sends* under and the counters it *receives* from its peer are
    # separate number spaces (nonce-uniqueness across directions is the crypto backend's
    # job). Receiving counter 1 must not consume the local send counter, and
    # sending must not mark a received counter as seen.
    s = _session()
    assert s.accept(1) is True         # peer's frame #1 received
    assert s.next_counter() == 1       # our own send counter is untouched
    assert s.next_counter() == 2
    assert s.accept(2) is True         # peer's frame #2 still fresh despite our sends
