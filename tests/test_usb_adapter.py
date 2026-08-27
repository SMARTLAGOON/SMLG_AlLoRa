"""The USB tunnel: a bridge board reached over its own USB socket, with no pins wired.

Three things are new relative to the GPIO serial tunnel, and each is covered here:

  * `_Stdio_port`, the console wrapped in Serial_link's port contract, because a native-USB
    board has no `machine.UART` behind its socket;
  * `Serial_link._resync`, which finds where a frame *starts*. The sentinel only says where one
    ends, so printable noise ahead of a frame used to be handed to the JSON parser and cost a
    good frame. This lives in the shared link, not in the USB path, because the pin rig has the
    same bug, just more rarely;
  * `USB_adapter`, which moves the library's debug output off stdout before anything can log,
    since on this board stdout is the wire.

The console is exercised over real OS pipes rather than a mock, so `select.poll`, the
non-blocking read and the flush are the real ones. What is not covered here is the same thing
the GPIO tunnel's smoke test does not cover: a real board.
"""
import json
import os
import threading

import pytest

from AlLoRa import tunnel_codec
from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Adapters.USB_adapter import USB_adapter, _File_sink
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Links.Loopback_link import Loopback_link
from AlLoRa.Links.Serial_link import Serial_link, _Stdio_port
from AlLoRa.utils import debug_utils

CONNCFG = {"name": "bridge", "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
           "tx_power": 14, "protocol_version": 3, "addressing": "sid", "timeout_delta": 0.1}


@pytest.fixture(autouse=True)
def restore_debug_sink():
    """The sink is module-global by design, so a test that installs one must put it back or
    every later test in the session runs silenced."""
    before = debug_utils.get_sink()
    yield
    debug_utils.set_sink(before)


@pytest.fixture
def pipes():
    """Two OS pipes standing in for the USB cable: one direction each, unbuffered, with real
    file descriptors so the port's `select.poll` is the real one."""
    opened = []

    def _pair():
        r, w = os.pipe()
        rf, wf = os.fdopen(r, "rb", 0), os.fdopen(w, "wb", 0)
        opened.extend((rf, wf))
        return rf, wf

    to_board_r, to_board_w = _pair()      # host -> board (the board's stdin)
    to_host_r, to_host_w = _pair()        # board -> host (the board's stdout)
    yield to_board_r, to_board_w, to_host_r, to_host_w
    for f in opened:
        try:
            f.close()
        except Exception:
            pass


# --- the invariant `_resync` stands on -------------------------------------------------------

def test_a_tunnel_codec_frame_contains_exactly_one_brace():
    # `_resync` finds a frame's start by looking for the last `{`. That is only correct while a
    # frame is a flat JSON object whose values are hex, numbers, null or short ASCII words. If
    # a nested object is ever added to the tunnel protocol, this fails first and loudly, rather
    # than the tunnel losing frames on a board.
    frames = [
        tunnel_codec.encode_transmit(b"\x2a\x00hello"),
        tunnel_codec.encode_listen(12),
        tunnel_codec.encode_exchange(b"\x2a\x00hi", 8, b"\x2a"),
        tunnel_codec.encode_set_rf(freq=868, sf=9, bw=125, cr=1, tx=14),
        tunnel_codec.encode_get_rf(),
        tunnel_codec.encode_get_mac(),
        tunnel_codec.encode_bool_reply(True),
        tunnel_codec.encode_listen_reply(b"\xff\x00payload", 1.25),
        tunnel_codec.encode_exchange_reply(b"\xff\x00payload", 1.25, "ok"),
        tunnel_codec.encode_exchange_reply(None, 8.0, "timeout"),
        tunnel_codec.encode_get_rf_reply([868, 7, 125, 1, 14]),
        tunnel_codec.encode_get_mac_reply("9eeff0dc"),
    ]
    for frame in frames:
        assert frame.count(b"{") == 1, frame
        assert frame.startswith(b"{"), frame


# --- _resync ---------------------------------------------------------------------------------

def _link():
    """A link with no port: enough to exercise the framing, which is where resync lives."""
    return Serial_link(port=object())


def test_resync_strips_the_boot_chatter_a_board_prints_on_its_way_up():
    link = _link()
    frame = tunnel_codec.encode_bool_reply(True)
    chatter = b"ESP-ROM:esp32s3-20210327\r\nMicroPython v1.24.1 on 2026-07-14; ESP32S3\r\n"
    assert link._resync(chatter + frame) == frame


def test_resync_leaves_a_clean_frame_exactly_as_it_found_it():
    link = _link()
    frame = tunnel_codec.encode_listen_reply(b"\x01\x02", 0.5)
    assert link._resync(frame) == frame
    assert link.preamble_dropped == 0


def test_resync_recovers_when_the_preamble_carries_a_brace_of_its_own():
    # Taking the first `{` would keep the debug line and still fail to parse; taking the last
    # one is what makes this recoverable.
    link = _link()
    frame = tunnel_codec.encode_get_mac_reply("9eeff0dc")
    assert link._resync(b'Adapter error: {"stray": 1} not a frame' + frame) == frame


def test_resync_reports_how_much_it_dropped():
    link = _link()
    frame = tunnel_codec.encode_bool_reply(False)
    link._resync(b"noise" + frame)
    link._resync(b"more noise" + frame)
    assert link.preamble_dropped == len(b"noise") + len(b"more noise")


def test_a_frame_with_nothing_frame_shaped_in_it_is_passed_through_untouched():
    # Emptying it here would turn "the far side sent garbage" into "the far side sent nothing",
    # and the caller could no longer tell a corrupt link from a silent one.
    link = _link()
    assert link._resync(b"total garbage") == b"total garbage"
    assert link.preamble_dropped == 0


def test_the_frame_reader_returns_a_clean_frame_despite_a_printable_preamble(pipes):
    # The regression in full: before resync this returned banner+JSON and the decode threw.
    to_board_r, _, to_host_r, to_host_w = pipes
    host = Serial_link(_Stdio_port(stdin=to_host_r, stdout=to_host_w))
    frame = tunnel_codec.encode_get_rf_reply([868, 7, 125, 1, 14])
    to_host_w.write(b"I (31) boot: ESP-IDF 5.2.0\r\n" + frame + Serial_link.SENTINEL)
    got = host.read_request(timeout=2)
    assert tunnel_codec.decode_get_rf_reply(got) == [868, 7, 125, 1, 14]


# --- the console as a port -------------------------------------------------------------------

def test_the_console_port_moves_bytes_in_both_directions(pipes):
    to_board_r, to_board_w, to_host_r, to_host_w = pipes
    board = _Stdio_port(stdin=to_board_r, stdout=to_host_w)
    to_board_w.write(b"a request")
    assert board.any() > 0
    assert board.read(64) == b"a request"
    board.write(b"a reply")
    assert to_host_r.read(7) == b"a reply"


def test_the_console_port_never_blocks_on_an_empty_console(pipes):
    # A blocking console read would stall the Adapter's serve loop past its own timeout and
    # leave the bridge deaf to the next request and to Ctrl-C.
    to_board_r, _, _, to_host_w = pipes
    board = _Stdio_port(stdin=to_board_r, stdout=to_host_w)
    assert board.any() == 0
    assert board.read(64) == b""


def test_the_console_port_reads_only_what_arrived(pipes):
    to_board_r, to_board_w, _, to_host_w = pipes
    board = _Stdio_port(stdin=to_board_r, stdout=to_host_w)
    to_board_w.write(b"five!")
    assert board.read(64) == b"five!"      # asked for 64, stopped at what was there


def test_bridge_usb_drops_what_a_host_sent_before_the_board_was_ready(pipes):
    to_board_r, to_board_w, _, to_host_w = pipes
    to_board_w.write(b"a request nobody was listening for" + Serial_link.SENTINEL)
    link = Serial_link.bridge_usb(stdin=to_board_r, stdout=to_host_w)
    assert link.read_request(timeout=0.2) is None


# --- the whole tunnel, over the console ------------------------------------------------------

def _usb_pair(pipes):
    """A host link and a bridge link facing each other across the two pipes, each holding the
    same console port the real thing holds."""
    to_board_r, to_board_w, to_host_r, to_host_w = pipes
    host = Serial_link(_Stdio_port(stdin=to_host_r, stdout=to_board_w))
    bridge = Serial_link.bridge_usb(stdin=to_board_r, stdout=to_host_w)
    return host, bridge


def test_a_transport_verb_crosses_the_usb_tunnel_and_reaches_the_radio(pipes):
    peer, bridge_radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    bridge_radio.config(CONNCFG)
    host_link, bridge_link = _usb_pair(pipes)
    adapter = Adapter(bridge_radio, link=bridge_link)

    stop = threading.Event()
    pump = threading.Thread(target=lambda: adapter.serve(should_stop=stop.is_set), daemon=True)
    pump.start()
    try:
        conn = Tunnel_connector(link=host_link)
        conn.config(CONNCFG)
        assert conn.transmit(b"\x2a\x00hello over usb") is True
        assert peer.recv(2.0) == b"\x2a\x00hello over usb"     # crossed console + radio unparsed
        assert conn.get_rf_config() == [868, 7, 125, 1, 14]
    finally:
        stop.set()
        pump.join(timeout=2)


def test_the_tunnel_survives_a_board_that_prints_its_banner_mid_session(pipes):
    """A bridge that browns out and reboots re-prints its banner into a live session.

    Two different guards catch that, depending on when it lands, and this is the first: chatter
    that arrives while the link is idle is dropped by the flush `rpc` already does before every
    request, so it never reaches the frame reader at all. Chatter that arrives after the request
    went out, while the reply is in flight, has no flush ahead of it and is `_resync`'s job;
    `test_the_frame_reader_returns_a_clean_frame_despite_a_printable_preamble` covers that half.
    Both paths must end with the transfer still running, which is what this asserts.
    """
    peer, bridge_radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    bridge_radio.config(CONNCFG)
    to_board_r, to_board_w, to_host_r, to_host_w = pipes
    host_link, bridge_link = _usb_pair(pipes)
    adapter = Adapter(bridge_radio, link=bridge_link)

    stop = threading.Event()
    pump = threading.Thread(target=lambda: adapter.serve(should_stop=stop.is_set), daemon=True)
    pump.start()
    try:
        conn = Tunnel_connector(link=host_link)
        conn.config(CONNCFG)
        assert conn.transmit(b"\x2a\x00before") is True
        assert peer.recv(2.0) == b"\x2a\x00before"
        to_host_w.write(b"\r\nMicroPython v1.24.1 on 2026-07-14; ESP32S3 with ESP32S3\r\n")
        assert conn.transmit(b"\x2a\x00after") is True
        assert peer.recv(2.0) == b"\x2a\x00after"
    finally:
        stop.set()
        pump.join(timeout=2)


# --- the adapter keeps its own logging off the wire ------------------------------------------

def _config(tmp_path, **adapter_block):
    path = tmp_path / "LoRa.json"
    path.write_text(json.dumps({
        "name": "USB", "debug": True, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3,
        "connector": {"freq": 868, "sf": 7, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1},
        "adapter": adapter_block,
    }))
    return str(path)


def test_booting_a_usb_adapter_writes_nothing_to_the_wire(tmp_path, capsys, pipes):
    # boot() announces the radio's MAC before it ever reaches setup_link. On this board that
    # line goes into the tunnel, so the sink has to be installed by the constructor, not by
    # setup_link. Anything on stdout here is a corrupted first frame on real hardware.
    _, _, _, to_host_w = pipes
    _, radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    link = Serial_link(_Stdio_port(stdin=pipes[0], stdout=to_host_w))
    USB_adapter(radio, config_file=_config(tmp_path), link=link)
    assert capsys.readouterr().out == ""


def test_a_usb_adapter_silences_the_radio_under_it_too(tmp_path, pipes):
    # The loudest logger on a bridge board is the radio Connector, not the Adapter, which is why
    # the sink is the library's and not the Adapter's own.
    _, _, _, to_host_w = pipes
    _, radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    link = Serial_link(_Stdio_port(stdin=pipes[0], stdout=to_host_w))
    USB_adapter(radio, config_file=_config(tmp_path), link=link)
    assert debug_utils.get_sink() is debug_utils.silence


def test_a_supplied_link_survives_the_boot(tmp_path, pipes):
    _, _, _, to_host_w = pipes
    _, radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    link = Serial_link(_Stdio_port(stdin=pipes[0], stdout=to_host_w))
    adapter = USB_adapter(radio, config_file=_config(tmp_path), link=link)
    assert adapter.link is link


def test_the_file_sink_is_opt_in_and_collects_the_lines(tmp_path, pipes):
    _, _, _, to_host_w = pipes
    _, radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    link = Serial_link(_Stdio_port(stdin=pipes[0], stdout=to_host_w))
    log = tmp_path / "adapter.log"
    USB_adapter(radio, config_file=_config(tmp_path, log="file", log_path=str(log)), link=link)
    debug_utils.print("a line the bridge wanted to keep")
    assert "a line the bridge wanted to keep" in log.read_text()


def test_a_log_that_cannot_be_written_does_not_stop_the_bridge(tmp_path):
    # A full or read-only flash must cost a log line, never the radio.
    sink = _File_sink(str(tmp_path / "no-such-dir" / "adapter.log"))
    sink("this goes nowhere, quietly")


def test_a_usb_adapter_logs_nothing_by_default(tmp_path, capsys, pipes):
    _, _, _, to_host_w = pipes
    _, radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    link = Serial_link(_Stdio_port(stdin=pipes[0], stdout=to_host_w))
    USB_adapter(radio, config_file=_config(tmp_path), link=link)
    debug_utils.print("this must not reach the console")
    assert capsys.readouterr().out == ""


# --- the host half: where the tunnel endpoint has to be written -------------------------------
#
# The host is the same `Serial_connector` for a USB tunnel as for a GPIO one, so nothing here is
# USB-specific except the path. What is worth pinning down is *where the endpoint keys live*,
# because a node hands its connector only the `connector` sub-block: a `serial_port` written
# anywhere else in LoRa.json is silently ignored and the tunnel opens the default port instead,
# which looks like a dead cable rather than a typo. None of this was covered before.

class _Opened:
    """Records what Serial_link.client was asked to open, instead of opening it."""

    def __init__(self):
        self.calls = []

    def __call__(self, serial_port, baud=9600, timeout=1, **kwargs):
        self.calls.append({"serial_port": serial_port, "baud": baud, "timeout": timeout})
        client, _bridge = Loopback_link.create_pair()
        return client


@pytest.fixture
def opened(monkeypatch):
    from AlLoRa.Links.Serial_link import Serial_link as SL
    recorder = _Opened()
    monkeypatch.setattr(SL, "client", recorder)
    return recorder


def test_the_tunnel_endpoint_is_read_from_the_connector_block(opened):
    from AlLoRa.Connectors.Serial_connector import Serial_connector
    conn = Serial_connector()
    conn.config(dict(CONNCFG, serial_port="/dev/ttyACM0", baud=115200, timeout=2))
    assert opened.calls == [{"serial_port": "/dev/ttyACM0", "baud": 115200, "timeout": 2}]


def test_an_endpoint_written_outside_the_connector_block_never_arrives(opened):
    # The failure this guards: `serial_port` at the top level of LoRa.json rather than inside
    # `connector`. The node passes only the sub-block, so the connector opens /dev/ttyAMA3 and
    # the run dies looking like a cabling fault.
    from AlLoRa.Connectors.Serial_connector import Serial_connector
    conn = Serial_connector()
    conn.config(dict(CONNCFG))                      # no serial_port in the block at all
    assert opened.calls[0]["serial_port"] == "/dev/ttyAMA3"


def test_a_hub_boots_its_tunnel_from_the_connector_block(tmp_path, opened):
    # End to end through the real config path a bench run uses, which is what
    # docs/hw-artifacts/v3-tunnel-usb/hub_lora.json is shaped for.
    from AlLoRa.Nodes.Hub import Hub
    from AlLoRa.Connectors.Serial_connector import Serial_connector
    cfg = tmp_path / "hub_lora.json"
    cfg.write_text(json.dumps({
        "name": "R", "chunk_size": 200, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 42,
        "debug": False, "result_path": str(tmp_path / "Results"),
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "min_timeout": 0.5, "max_timeout": 12,
                      "timeout_delta": 1, "debug": False,
                      "serial_port": "/dev/cu.usbmodem101", "baud": 9600, "timeout": 1},
    }))
    Hub(Serial_connector(), config_file=str(cfg), nodes_file=None)
    assert opened.calls[0]["serial_port"] == "/dev/cu.usbmodem101"
