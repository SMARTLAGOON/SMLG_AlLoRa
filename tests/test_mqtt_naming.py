"""The MQTT envelope — the pairing convention between MQTT_DataSource and MQTT_DataSink.

The original topic (+ artifact id + timestamp) rides the AlLoRa file NAME, which crosses
the link once inside METADATA — the payload stays the MQTT bytes verbatim, so preserving
the topic costs zero bytes per chunk. The name must also survive being a filename: the
receiver builds its reassembly temp path from it, so an encoded name can never contain a
path separator.
"""
import pytest

from AlLoRa.DataSources.mqtt_naming import encode_name, decode_name, is_envelope


def test_round_trip_preserves_topic_id_and_timestamp():
    name = encode_name("sensors/greenhouse/temp", 7, 123456789)
    assert is_envelope(name)
    assert decode_name(name) == ("sensors/greenhouse/temp", 7, 123456789)


def test_round_trip_without_timestamp():
    # A device with no synced clock ships no timestamp rather than a fake one.
    name = encode_name("a/b", 1)
    assert decode_name(name) == ("a/b", 1, None)


def test_encoded_name_is_filesystem_safe_and_escapes_survive():
    topic = "odd/t!o%p/ic"
    name = encode_name(topic, 42)
    assert "/" not in name
    assert decode_name(name)[0] == topic


def test_plain_filenames_are_not_envelopes():
    assert not is_envelope("temperature.json")
    assert not is_envelope("2026-07-19.jpg")


def test_malformed_envelope_raises():
    # Prefix collision with a real file name must fail loudly, so the sink can fall
    # back to plain-file handling instead of publishing to a garbage topic.
    with pytest.raises(ValueError):
        decode_name("mq!notanint!!topic")
    with pytest.raises(ValueError):
        decode_name("mq!7")
    with pytest.raises(ValueError):
        decode_name("mq!7!!abc%2")  # escape cut short at the end of the name
