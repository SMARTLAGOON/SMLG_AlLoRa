"""The ephemeral-static ECDH handshake that establishes a secure Session.

Three role-named steps, decoupled from the wire — each produces or consumes an opaque
payload of bytes, so the caller chooses how to frame them and no handshake wire-kind is
fixed here:

    initiator_hello(randfunc)                 -> (state, hello_payload)     # Source
    responder_accept(static_priv, hello, sid) -> (session, welcome_payload) # Collector
    initiator_complete(state, welcome)        -> session                    # Source

The initiator (a Source) makes a fresh ephemeral keypair per session; the responder (the
Collector) holds a long-lived static keypair and assigns the session id. Each derives the
same ECDH shared secret from its own private key and the peer's public key, and the KDF
turns it into matching session keys — the secret itself never crosses the wire. A fresh
ephemeral key per session means a reboot re-handshakes into a distinct key.

The two AEAD keys are combined into the Session's opaque ``key`` here; the frame layer
splits them back out when it calls the AEAD.
"""
from AlLoRa.Security.ec_p256 import (
    generate_private_key, public_key_uncompressed, ecdh_shared_secret,
)
from AlLoRa.Security.kdf import derive_session_material
from AlLoRa.Security.Session import Session

_PUB_LEN = 65  # SEC1 uncompressed public key: 0x04 || X(32) || Y(32)


def initiator_hello(randfunc):
    """Source: generate an ephemeral keypair. Returns (state, hello_payload), where state is
    the ephemeral private key held until initiator_complete() and hello_payload is the
    ephemeral public key to send."""
    ephemeral_priv = generate_private_key(randfunc)
    return ephemeral_priv, public_key_uncompressed(ephemeral_priv)


def responder_accept(static_priv, hello_payload, sid):
    """Collector: derive the shared secret from its static private key and the initiator's
    ephemeral public key, build the session under the assigned sid, and return
    (session, welcome_payload). The welcome carries the responder's static public key and
    the sid."""
    shared = ecdh_shared_secret(static_priv, hello_payload)
    session = _session_from(shared, sid, is_initiator=False)
    welcome_payload = public_key_uncompressed(static_priv) + bytes([sid])
    return session, welcome_payload


def initiator_complete(state, welcome_payload):
    """Source: read the responder's static public key and sid from the welcome, derive the
    same shared secret with the ephemeral private key, and build the matching session."""
    ephemeral_priv = state
    static_pub = welcome_payload[:_PUB_LEN]
    sid = welcome_payload[_PUB_LEN]
    shared = ecdh_shared_secret(ephemeral_priv, static_pub)
    return _session_from(shared, sid, is_initiator=True)


def _session_from(shared_secret, sid, is_initiator):
    # One shared AES+HMAC key, two per-direction nonce prefixes. Each end seals with its own
    # direction's prefix and opens the peer's with the other — so the two ends agree (the
    # initiator's send prefix is the responder's receive prefix, and vice versa) while their
    # nonce spaces stay disjoint.
    enc_key, mac_key, prefix_ab, prefix_ba = derive_session_material(shared_secret)
    key = enc_key + mac_key
    if is_initiator:   # sends initiator->responder (ab), receives responder->initiator (ba)
        return Session(sid=sid, key=key, send_nonce_prefix=prefix_ab, recv_nonce_prefix=prefix_ba)
    return Session(sid=sid, key=key, send_nonce_prefix=prefix_ba, recv_nonce_prefix=prefix_ab)
