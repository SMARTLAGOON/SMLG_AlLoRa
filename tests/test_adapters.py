"""The Adapters package: the bridge half of a split Connector, which is not a node.

An Adapter is what a bridge board's main.py instantiates. It owns a radio and a link, reads a
transport-verb request off the link, runs that verb on the radio, and writes the result back.
It holds no protocol logic, no files, no sessions and no keys, which is why it does not inherit
Node: the node-type set is exactly {Edge, Hub}.

The medium is in the class name (Serial_adapter / WiFi_adapter) and the radio is the argument,
mirroring the node side's WiFi_connector / Serial_connector. The two axes are orthogonal
because the radio varies per board independently of the link.
"""
import json
import threading

import pytest

from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Links.Loopback_link import Loopback_link

CONNCFG = {"name": "bridge", "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
           "tx_power": 14, "protocol_version": 3, "addressing": "sid", "timeout_delta": 0.1}


def _bridge(adapter):
    """Pump the adapter in a thread, the way a board's run() would, and hand back a stopper."""
    stop = threading.Event()
    pump = threading.Thread(target=lambda: adapter.serve(should_stop=stop.is_set), daemon=True)
    pump.start()
    return stop, pump


def _teardown(stop, pump):
    stop.set()
    pump.join(timeout=2)


# --- Seam 1: the base, constructed from a bare radio + link (no config file) ----------------

def test_adapter_serves_transport_verbs_from_a_bare_radio_and_link():
    # The tunnel tests build a bridge this way: no config file, no boot, just the two things a
    # bridge actually needs. The base must stay constructible like that.
    peer, bridge_radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    bridge_radio.config(CONNCFG)
    client_link, bridge_link = Loopback_link.create_pair()
    adapter = Adapter(bridge_radio, link=bridge_link)
    stop, pump = _bridge(adapter)
    try:
        conn = Tunnel_connector(link=client_link)
        conn.config(CONNCFG)
        assert conn.transmit(b"\x2a\x00hello") is True
        assert peer.recv(1.0) == b"\x2a\x00hello"      # crossed link + radio unparsed
    finally:
        _teardown(stop, pump)


# --- Seam 2: booting a board from its config file -------------------------------------------

class _ProbeAdapter(Adapter):
    """A medium that records the link block it was given instead of opening a real port."""

    def setup_link(self, config):
        self.link_config = config


def _write_config(path, link_block_key="adapter", protocol_version=3):
    config = {
        "name": "bridge", "debug": False, "mesh_mode": False, "short_mac": True,
        "protocol_version": protocol_version,
        "connector": {"freq": 869, "sf": 9, "bandwidth": 250, "coding_rate": 2,
                      "tx_power": 20, "timeout_delta": 0.1},
        link_block_key: {"uartid": 1, "baud": 115200},
    }
    with open(path, "w") as f:
        json.dump(config, f)
    return config


def test_booting_configures_the_radio_from_the_config_file(tmp_path):
    path = str(tmp_path / "LoRa.json")
    _write_config(path)
    radio = Loopback_connector("c3c3c3c3")

    adapter = _ProbeAdapter(radio, config_file=path)

    assert radio.frequency == 869 and radio.sf == 9
    assert radio.bw == 250 and radio.cr == 2 and radio.tx_power == 20
    # A v3 bridge frames the same way a v3 node's local radio would.
    assert radio.protocol_version == 3 and radio.addressing == "sid"
    assert adapter.status["MAC"] == "c3c3c3c3"


def test_a_booted_adapter_reports_the_rf_config_it_is_running_on(tmp_path):
    # A bridge board with a screen showed its RF config when the bridge was still a Node. It
    # owns the radio, so these are things it can genuinely see; only the transfer fields
    # (file, chunk, retransmissions) belong to the logic-holder and are legitimately absent.
    path = str(tmp_path / "LoRa.json")
    _write_config(path)

    adapter = _ProbeAdapter(Loopback_connector("c3c3c3c3"), config_file=path)

    assert adapter.status["Freq"] == 869
    assert adapter.status["SF"] == 9
    assert adapter.status["BW"] == 250
    assert adapter.status["CR"] == 2
    assert adapter.status["TX_P"] == 20


def test_booting_hands_the_adapter_block_to_the_medium(tmp_path):
    path = str(tmp_path / "LoRa.json")
    _write_config(path)

    adapter = _ProbeAdapter(Loopback_connector("c3c3c3c3"), config_file=path)

    assert adapter.link_config == {"uartid": 1, "baud": 115200}


def test_a_fielded_config_using_the_old_interface_key_still_boots(tmp_path):
    # Bridge boards in the field carry a LoRa.json whose link block is named "interface", the
    # pre-Adapters spelling. Renaming the key without a fallback would boot them with no port.
    path = str(tmp_path / "LoRa.json")
    _write_config(path, link_block_key="interface")

    adapter = _ProbeAdapter(Loopback_connector("c3c3c3c3"), config_file=path)

    assert adapter.link_config == {"uartid": 1, "baud": 115200}


def test_a_config_with_no_link_block_boots_with_an_empty_one(tmp_path):
    path = str(tmp_path / "LoRa.json")
    config = _write_config(path)
    del config["adapter"]
    with open(path, "w") as f:
        json.dump(config, f)

    adapter = _ProbeAdapter(Loopback_connector("c3c3c3c3"), config_file=path)

    assert adapter.link_config == {}


def test_a_v2_bridge_keeps_mac_addressing(tmp_path):
    path = str(tmp_path / "LoRa.json")
    _write_config(path, protocol_version=2)
    radio = Loopback_connector("c3c3c3c3")

    _ProbeAdapter(radio, config_file=path)

    assert radio.protocol_version == 2 and radio.addressing == "mac"


# --- Seam 3: the medium subclasses ----------------------------------------------------------
# The real ports are device-only (machine.UART, MicroPython network), so what is checkable on
# CPython is that each medium carries its config block through to its Link unchanged. The
# ports themselves are proven on hardware.

def test_serial_adapter_builds_its_link_from_the_uart_settings(tmp_path, monkeypatch):
    from AlLoRa.Adapters.Serial_adapter import Serial_adapter
    from AlLoRa.Links.Serial_link import Serial_link

    built = {}
    monkeypatch.setattr(Serial_link, "bridge",
                        classmethod(lambda cls, **kw: built.update(kw) or "LINK"))

    path = str(tmp_path / "LoRa.json")
    _write_config(path)
    adapter = Serial_adapter(Loopback_connector("c3c3c3c3"), config_file=path)

    assert adapter.link == "LINK"
    assert built["uartid"] == 1 and built["baud"] == 115200


def test_wifi_adapter_binds_its_link_to_the_configured_host_and_port(tmp_path, monkeypatch):
    from AlLoRa.Adapters.WiFi_adapter import WiFi_adapter
    from AlLoRa.Links.WiFi_link import WiFi_link

    built = {}
    monkeypatch.setattr(WiFi_link, "bridge",
                        classmethod(lambda cls, **kw: built.update(kw) or "LINK"))
    monkeypatch.setattr(WiFi_adapter, "init_wifi", lambda self: None)   # no radio hardware here

    path = str(tmp_path / "LoRa.json")
    config = _write_config(path)
    config["adapter"] = {"mode": "hotspot", "ssid": "AlLoRa-bridge", "psw": "secret",
                         "host": "192.168.4.1", "port": 8080}
    with open(path, "w") as f:
        json.dump(config, f)

    adapter = WiFi_adapter(Loopback_connector("c3c3c3c3"), config_file=path)

    assert adapter.link == "LINK"
    assert built == {"host": "192.168.4.1", "port": 8080}
    assert adapter.mode == "hotspot" and adapter.ssid == "AlLoRa-bridge"


# --- Seam 4: run(), the verb a board's main.py calls -----------------------------------------

def test_run_serves_the_channel_and_returns_when_its_timeout_expires():
    peer, bridge_radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    bridge_radio.config(CONNCFG)
    client_link, bridge_link = Loopback_link.create_pair()
    adapter = Adapter(bridge_radio, link=bridge_link)

    served = threading.Thread(target=lambda: adapter.run(timeout=3), daemon=True)
    served.start()
    conn = Tunnel_connector(link=client_link)
    conn.config(CONNCFG)
    assert conn.transmit(b"\x2a\x00hi") is True

    # A deployed bridge passes nothing and loops forever; the timeout is what makes the loop
    # testable, exactly as it is on Edge.run / Hub.run.
    served.join(timeout=10)
    assert not served.is_alive(), "run(timeout=...) did not return"


def test_run_reports_the_signal_of_the_frame_it_just_handled():
    peer, bridge_radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    bridge_radio.config(CONNCFG)
    client_link, bridge_link = Loopback_link.create_pair()
    adapter = Adapter(bridge_radio, link=bridge_link)

    seen = []

    class _Screen:
        def update(self, values):
            seen.append(dict(values))

    adapter.register_subscriber(_Screen())
    pump = threading.Thread(target=lambda: adapter.run(timeout=3), daemon=True)
    pump.start()
    conn = Tunnel_connector(link=client_link)
    conn.config(CONNCFG)
    conn.transmit(b"\x2a\x00hi")
    pump.join(timeout=10)

    assert seen, "a served request notified no subscriber"
    assert seen[-1]["RSSI"] != "-" and seen[-1]["SNR"] != "-"


# --- Seam 5: the structure the pass is for ---------------------------------------------------

def test_an_adapter_is_not_a_node():
    # The node-type set is exactly {Edge, Hub}. A bridge runs no protocol, holds no files and
    # no keys, so it has no business inheriting the node base.
    from AlLoRa.Nodes.Node import Node
    assert not issubclass(Adapter, Node)


def test_the_interfaces_package_is_gone():
    # "Interface" left the vocabulary as a delete, not a rename: the bridge half is an Adapter.
    with pytest.raises(ImportError):
        __import__("AlLoRa.Interfaces.Tunnel_interface")
