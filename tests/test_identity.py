"""Unit — the device_id fingerprint (a v3 Source's crypto-bound identity).

A v3 Source's identity is the fingerprint of its long-term public key: device_id =
SHA256(pubkey). It is the successor to MAC-as-identity — registered on the Collector like a
MAC, but derived from a key so it can't be spoofed by claiming someone else's address. The
first 4 bytes address first contact on the wire; the first byte seeds the session id.
"""
import hashlib

from AlLoRa.Security.ec_p256 import generate_private_key, public_key_uncompressed
from AlLoRa.Security.identity import device_id_from_pubkey, load_or_create_identity
from test_config_persistence import _dying_open


def test_device_id_is_the_sha256_of_the_public_key():
    priv = generate_private_key(lambda n: b"\x01" * n)
    pub = public_key_uncompressed(priv)

    device_id = device_id_from_pubkey(pub)

    assert device_id == hashlib.sha256(pub).digest()
    assert len(device_id) == 32


def test_device_id_is_deterministic_for_a_key_and_distinct_across_keys():
    pub_a = public_key_uncompressed(generate_private_key(lambda n: b"\x02" * n))
    pub_b = public_key_uncompressed(generate_private_key(lambda n: b"\x03" * n))

    assert device_id_from_pubkey(pub_a) == device_id_from_pubkey(pub_a)
    assert device_id_from_pubkey(pub_a) != device_id_from_pubkey(pub_b)


def test_a_key_written_part_way_is_never_adopted_as_an_identity(tmp_path, monkeypatch):
    # The quietest failure of the three. The scalar is stored as 64 hex characters and read
    # back with int(..., 16), so a file cut in half still parses, still yields a valid private
    # key, and still gives a stable device_id. The node comes back working and registered while
    # carrying half the entropy it was generated with, and nothing anywhere says so. Losing the
    # file outright is the better outcome: a node with no key generates one.
    path = str(tmp_path / "identity.key")
    monkeypatch.setattr("builtins.open", _dying_open)

    try:
        load_or_create_identity(path, lambda n: b"\x04" * n)
    except OSError:
        pass                     # the write failed; what matters is what it left behind
    monkeypatch.undo()

    try:
        with open(path) as f:
            stored = f.read().strip()
    except OSError:
        return                   # no key at all: the next boot generates one, which is correct
    assert len(stored) == 64, \
        "a key file a later boot will accept must be the whole key, never part of one"
