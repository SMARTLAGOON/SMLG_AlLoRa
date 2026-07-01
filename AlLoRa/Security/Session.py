"""The per-peer secure-session state: sid + key + counter, crypto-agnostic.

Created by the handshake (ECDH -> KDF -> key + nonce prefix) and consumed by the secure
transfer. The Session owns the two stateful pieces the wire depends on — the monotonic
send counter and the receive-side anti-replay window — and holds the key material as
opaque bytes so the AEAD backend stays swappable. It fixes no nonce/AAD byte layout (that
is intentionally left to a later crypto-review pass, since the exact bytes are a published
surface we don't want to freeze prematurely); it only guarantees a unique, monotone send
counter and correct replay rejection.
"""
from AlLoRa.Security.Replay_window import Replay_window


class SessionExhausted(Exception):
    """The 2-byte send counter is spent. Sending another frame would reuse a counter — and
    thus a (key, nonce) pair — so the node must re-handshake for a fresh key + nonce prefix
    before it can send again."""


class Session:

    MAX_COUNTER = 0xFFFF   # 2-byte wire field; 0 is reserved as the "no counter yet" sentinel

    def __init__(self, sid, key, send_nonce_prefix, recv_nonce_prefix):
        self.sid = sid
        self.key = key                    # opaque AES+HMAC key material (shared both directions)
        # Per-direction nonce prefixes (never transmitted): I seal with send_, I open a peer
        # frame with recv_ (= the peer's send_). Two prefixes keep the directions' nonce spaces
        # disjoint even though both counters start at 1 — no cross-direction (key, nonce) reuse.
        self.send_nonce_prefix = send_nonce_prefix
        self.recv_nonce_prefix = recv_nonce_prefix
        self._send_counter = 0
        self._replay = Replay_window()

    def next_counter(self):
        """Return the next send counter (1..MAX_COUNTER), strictly increasing. Raises
        SessionExhausted rather than wrapping — a wrapped counter reuses a nonce."""
        if self._send_counter >= self.MAX_COUNTER:
            raise SessionExhausted("send counter exhausted for session {}".format(self.sid))
        self._send_counter += 1
        return self._send_counter

    def counter_exhausted(self):
        """True once the send counter can issue no more values — the node should
        re-handshake. Check this *before* next_counter() to re-key without dropping a frame."""
        return self._send_counter >= self.MAX_COUNTER

    def accept(self, counter):
        """Receive side: True if an inbound frame's counter is fresh (record it), False if
        it is a replay or too old. Independent of the send counter — the peer counts its
        own frames."""
        return self._replay.accept(counter)
