"""Unit — the ephemeral-static ECDH handshake produces a shared session.

The initiator (a Source) makes a fresh ephemeral keypair per session; the responder (the
Collector) holds a long-lived static keypair and assigns the session id. They exchange
public keys, each computes the same ECDH shared secret, and the KDF turns it into matching
session keys — so both ends end up holding the *same* Session (same key material, same
nonce prefix, same sid) without the secret ever crossing the wire.

The handshake is decoupled from the wire: its three steps produce and consume opaque
payload bytes, so the framing (which packet carries them) is the caller's concern and no
handshake wire-kind is frozen here. The pay-off is proven by composing with the AEAD:
a frame sealed under the initiator's session opens under the responder's.
"""
import os
import threading

from AlLoRa.Security.handshake import initiator_hello, responder_accept, initiator_complete
from AlLoRa.Security.ec_p256 import generate_private_key
from AlLoRa.Security.kdf import ENC_KEY_LEN, MAC_KEY_LEN
from AlLoRa.Security.AEAD import detect_aead
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Connectors.Loopback_connector import Loopback_connector

SID = 17
SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"


def _split(key):
    return key[:ENC_KEY_LEN], key[ENC_KEY_LEN:ENC_KEY_LEN + MAC_KEY_LEN]


def _run_handshake():
    responder_static_priv = generate_private_key(os.urandom)   # the Collector's long-lived key
    state, hello = initiator_hello(os.urandom)                 # Source -> Collector
    session_r, welcome = responder_accept(responder_static_priv, hello, SID)  # Collector -> Source
    session_i = initiator_complete(state, welcome)             # Source finalizes
    return session_i, session_r


def test_handshake_yields_matching_session_keys():
    session_i, session_r = _run_handshake()
    assert session_i.sid == session_r.sid == SID
    assert session_i.key == session_r.key
    # Per-direction prefixes agree across the ends: what one seals under, the other opens under.
    assert session_i.send_nonce_prefix == session_r.recv_nonce_prefix
    assert session_i.recv_nonce_prefix == session_r.send_nonce_prefix
    assert session_i.send_nonce_prefix != session_i.recv_nonce_prefix   # the two directions differ


def test_a_frame_sealed_by_one_end_opens_at_the_other():
    session_i, session_r = _run_handshake()
    aead = detect_aead()

    # The initiator seals under its send prefix; the responder opens under its recv prefix,
    # which the handshake made equal — so the same nonce reconstructs at both ends.
    nonce = session_i.send_nonce_prefix + b"\x00\x01"   # prefix + a frame counter
    aad = b"frame-header"
    plaintext = b"a sensor reading"

    enc_i, mac_i = _split(session_i.key)
    sealed = aead.seal(enc_i, mac_i, nonce, aad, plaintext)

    enc_r, mac_r = _split(session_r.key)
    assert session_r.recv_nonce_prefix == session_i.send_nonce_prefix
    assert aead.open(enc_r, mac_r, nonce, aad, sealed) == plaintext


def test_two_handshakes_produce_different_sessions():
    # Fresh ephemeral key each time -> a new session key each session (forward secrecy for
    # the data path; a reboot re-handshakes into a distinct key).
    a_i, _ = _run_handshake()
    b_i, _ = _run_handshake()
    assert a_i.key != b_i.key


# --- acceptance: the handshake flowing over the real transport seam ---------

def _send_ctrl(conn, src_mac, dst_mac, payload):
    # Frame a handshake payload as a MAC-addressed v3 CTRL packet (no session id exists yet).
    p = Packet_v3(addressing="mac")
    p.set_source(src_mac)
    p.set_destination(dst_mac)
    p.set_kind(Packet_v3.CTRL)
    p.set_payload(payload)
    conn.send(p)


def _recv_ctrl(conn):
    raw = conn.recv(focus_time=5)
    assert raw is not None, "handshake frame never arrived"
    p = Packet_v3(addressing="mac")
    assert p.load(raw) is True, "handshake frame failed to parse/verify"
    return p.get_payload()


def test_handshake_completes_over_the_loopback_connector():
    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    responder_static_priv = generate_private_key(os.urandom)   # the Collector's long-lived key
    box, errors = {}, []

    def collector():
        try:
            hello = _recv_ctrl(collector_conn)
            session_r, welcome = responder_accept(responder_static_priv, hello, SID)
            _send_ctrl(collector_conn, COLLECTOR_MAC, SOURCE_MAC, welcome)
            box["session_r"] = session_r
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    t = threading.Thread(target=collector, name="collector-handshake", daemon=True)
    t.start()

    state, hello = initiator_hello(os.urandom)
    _send_ctrl(source_conn, SOURCE_MAC, COLLECTOR_MAC, hello)
    welcome = _recv_ctrl(source_conn)
    session_i = initiator_complete(state, welcome)
    t.join(timeout=10)

    assert not errors, "collector raised: {}".format(errors)
    assert not t.is_alive(), "collector did not finish the handshake"
    session_r = box["session_r"]

    # both ends built the same session without the secret ever crossing the wire
    assert session_i.key == session_r.key
    assert session_i.send_nonce_prefix == session_r.recv_nonce_prefix
    assert session_i.recv_nonce_prefix == session_r.send_nonce_prefix
    assert session_i.sid == session_r.sid == SID

    # and a frame sealed by one end opens at the other, end to end over the connector
    aead = detect_aead()
    enc_i, mac_i = _split(session_i.key)
    enc_r, mac_r = _split(session_r.key)
    nonce = session_i.send_nonce_prefix + b"\x00\x01"
    sealed = aead.seal(enc_i, mac_i, nonce, b"hdr", b"payload-over-the-wire")
    assert aead.open(enc_r, mac_r, nonce, b"hdr", sealed) == b"payload-over-the-wire"
