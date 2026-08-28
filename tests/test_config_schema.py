"""The config file decides what a board is, and what it is called.

Two rules, and both are about a board somebody cannot reach.

**Both filenames are read forever.** `LoRa.json` named the one section of the file that is
actually about the radio; the file also carries the posture, the identity path, the control
root, where results land and what the board is, so the name moved to `AlLoRa.json`. The old
name is not a migration window. There are deployed boards this project has no physical access
to, student repositories vendoring the library, and a published paper trail, and a name that
stops being read is a node that stops booting for a reason nobody at the antenna can see.

**A chunk size nobody chose is not persisted.** `chunk_size` used to default to 235, which was
v2's spreading-factor-dependent ceiling sitting in front of a computation that already knew the
real answer, so it could only ever cut a file smaller than the link would carry. Omitting the
key now means "the largest chunk my frames can hold", and the write-back has to leave it
omitted or the next boot finds a constant nobody wrote.
"""
import json
import os

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.utils.file_utils import resolve_config_file

EDGE_MAC = "a1a1a1a1"


def _config(**overrides):
    config = {
        "name": "S",
        "node": "edge",
        "mesh_mode": False,
        "short_mac": True,
        "protocol_version": 3,
        "security_mode": "open",
        "session_id": 42,
        "debug": False,
        "connector": {
            "driver": "sx127x",
            "sf": 7,
            "freq": 868,
            "bandwidth": 125,
            "coding_rate": 1,
            "tx_power": 14,
            "timeout_delta": 0.1,
            "debug": False,
        },
    }
    config.update(overrides)
    return config


def _write(path, config):
    with open(str(path), "w") as f:
        f.write(json.dumps(config, indent=2))


def _edge(tmp_path, monkeypatch, **overrides):
    monkeypatch.chdir(tmp_path)
    return Edge(Loopback_connector(EDGE_MAC), **overrides)


# --- D1: the filename -----------------------------------------------------------------------

def test_a_node_boots_from_the_current_config_name(tmp_path, monkeypatch):
    _write(tmp_path / "AlLoRa.json", _config())
    edge = _edge(tmp_path, monkeypatch)
    assert edge.config_file == "AlLoRa.json"


def test_a_board_still_carrying_the_old_name_boots_from_it(tmp_path, monkeypatch):
    """The whole reason the fallback exists: a node in the field, out of reach, never updated."""
    _write(tmp_path / "LoRa.json", _config())
    edge = _edge(tmp_path, monkeypatch)
    assert edge.config_file == "LoRa.json"


def test_the_current_name_wins_when_a_board_carries_both(tmp_path, monkeypatch):
    """A board part way through an update has both. It has to come up on the newer one, or the
    update it was given never takes effect and nothing says so."""
    _write(tmp_path / "LoRa.json", _config(name="old"))
    _write(tmp_path / "AlLoRa.json", _config(name="new"))
    edge = _edge(tmp_path, monkeypatch)
    assert edge.config_file == "AlLoRa.json"
    assert edge.name == "new"


def test_a_named_config_file_is_the_one_used(tmp_path, monkeypatch):
    """Naming a file overrides the search. Deployments that hold several node configs in one
    directory depend on this, and so does every test in this suite."""
    _write(tmp_path / "AlLoRa.json", _config(name="ignored"))
    _write(tmp_path / "rooftop.json", _config(name="rooftop"))
    edge = _edge(tmp_path, monkeypatch, config_file="rooftop.json")
    assert edge.name == "rooftop"


def test_no_config_file_at_all_says_what_it_looked_for(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(OSError) as excinfo:
        resolve_config_file()
    assert "AlLoRa.json" in str(excinfo.value) and "LoRa.json" in str(excinfo.value)


def test_a_node_writes_back_the_file_it_read(tmp_path, monkeypatch):
    """ADR 0016's rule, which the second filename could quietly break: a board booted from the
    old name must not have its settled radio written to a new file it will never read."""
    _write(tmp_path / "LoRa.json", _config())
    edge = _edge(tmp_path, monkeypatch)
    edge.backup_config()
    assert not os.path.exists(str(tmp_path / "AlLoRa.json"))
    written = json.load(open(str(tmp_path / "LoRa.json")))
    assert written["connector"]["sf"] == 7


# --- D4: the chunk size ---------------------------------------------------------------------

def test_an_omitted_chunk_size_means_the_computed_ceiling(tmp_path, monkeypatch):
    _write(tmp_path / "AlLoRa.json", _config())
    edge = _edge(tmp_path, monkeypatch)
    assert edge.chunk_size == edge.calculate_max_chunk_size()


def test_an_omitted_chunk_size_is_not_written_back(tmp_path, monkeypatch):
    """Persisting the computed answer would turn "give me the most you can carry" into a
    constant, and the node would keep it when the arithmetic improves with nothing to say so."""
    _write(tmp_path / "AlLoRa.json", _config())
    edge = _edge(tmp_path, monkeypatch)
    edge.backup_config()
    assert "chunk_size" not in json.load(open(str(tmp_path / "AlLoRa.json")))


def test_a_chosen_chunk_size_is_honoured_and_persisted(tmp_path, monkeypatch):
    _write(tmp_path / "AlLoRa.json", _config(chunk_size=100))
    edge = _edge(tmp_path, monkeypatch)
    assert edge.chunk_size == 100
    edge.backup_config()
    assert json.load(open(str(tmp_path / "AlLoRa.json")))["chunk_size"] == 100


def test_a_chosen_chunk_size_is_still_clamped_to_what_the_frames_carry(tmp_path, monkeypatch):
    _write(tmp_path / "AlLoRa.json", _config(chunk_size=9000))
    edge = _edge(tmp_path, monkeypatch)
    assert edge.chunk_size == edge.calculate_max_chunk_size()


def test_a_commanded_chunk_size_is_written_back_even_when_the_config_omitted_one(
        tmp_path, monkeypatch):
    """A node that took its ceiling from the config's silence and was then told a number over
    the air has to keep that number, or the next reboot undoes the command."""
    _write(tmp_path / "AlLoRa.json", _config())
    edge = _edge(tmp_path, monkeypatch)
    assert edge.change_rf_config({"cks": 120})
    edge.backup_config()
    assert json.load(open(str(tmp_path / "AlLoRa.json")))["chunk_size"] == 120
