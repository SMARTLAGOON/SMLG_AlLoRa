"""A deployment says which of its board's peripherals it wants on, and the board says what it has.

Two different questions, and conflating them is what produced a forked `main_pro.py` per board.
Only the board file can say whether there is a screen soldered to it; only the operator can say
whether this deployment wants that screen drawing. So the board declares `HAS_SCREEN`, `HAS_SD`
and `HAS_LED`, the config's `device` section says which to switch on, and saying nothing gets you
everything the board has.

The library is not involved in any of it. A screen and a log reach the node through
`register_subscriber`, which takes anything with an `update` method, and the card is a folder
once the board has mounted it. Nothing under `AlLoRa/` learns what a screen is.
"""
import json
import os
import sys
import types

import pytest

from test_generic_main import _load_generic, _config

generic = _load_generic()


class FakeBoard:
    HAS_SCREEN = True
    HAS_SD = True
    HAS_LED = True
    SD_MOUNT_POINT = "/sd"

    def __init__(self, mount_raises=False):
        self.mount_raises = mount_raises
        self.mounted_at = None

    def mount_sd(self, path=None):
        if self.mount_raises:
            raise OSError("no SD card")
        self.mounted_at = path or self.SD_MOUNT_POINT
        return self.mounted_at


class ScreenlessBoard(FakeBoard):
    HAS_SCREEN = False


@pytest.fixture
def board(monkeypatch):
    """Hand the program a board without importing a board module that needs `machine`."""
    made = FakeBoard()
    monkeypatch.setattr(generic, "build_board", lambda name: made)
    return made


@pytest.fixture
def fake_board_modules(monkeypatch):
    """Stand in for the frozen `board/` package, which only exists on a flashed device."""
    registered = {"screens": [], "leds": []}

    class FakeScreen:
        def __init__(self, board, img_data=None, layout_config=None, button=False):
            self.board = board
            self.img_data = img_data
            self.layout_config = layout_config
            self.button = button
            registered["screens"].append(self)

        def update(self, status):
            pass

    class FakeLED:
        def __init__(self, board):
            self.board = board
            self.running = False
            registered["leds"].append(self)

        def run(self):
            self.running = True

    package = types.ModuleType("board")
    package.__path__ = []
    oled = types.ModuleType("board.oled_screen")
    oled.OLED_Screen = FakeScreen
    led = types.ModuleType("board.led_alive")
    led.LED = FakeLED
    monkeypatch.setitem(sys.modules, "board", package)
    monkeypatch.setitem(sys.modules, "board.oled_screen", oled)
    monkeypatch.setitem(sys.modules, "board.led_alive", led)
    return registered


class _Node:
    def __init__(self):
        self.subscribers = []
        self.notified = 0

    def register_subscriber(self, subscriber):
        self.subscribers.append(subscriber)

    def notify_subscribers(self):
        self.notified += 1


# -- what the board has, and what the deployment wants ------------------------------------


def test_saying_nothing_gets_what_the_board_has():
    assert generic.wants({}, "screen", True) is True
    assert generic.wants({}, "screen", False) is False


def test_a_deployment_can_turn_off_something_the_board_has():
    assert generic.wants({"screen": False}, "screen", True) is False


def test_asking_for_what_the_board_does_not_have_stops_instead_of_pretending():
    with pytest.raises(SystemExit) as e:
        generic.wants({"board": "t3s3", "screen": True}, "screen", False)
    assert "screen" in str(e.value)


def test_an_unknown_board_halts_and_names_the_ones_it_builds():
    with pytest.raises(SystemExit) as e:
        generic.build_board("heltec-v3")
    message = str(e.value)
    assert "heltec-v3" in message and "t3s3" in message


# -- no device section is the ordinary case, and costs nothing ----------------------------


def test_a_config_with_no_device_section_builds_no_board():
    board, notes = generic.prepare_device(_config())
    assert board is None and notes == []


def test_wiring_without_a_board_registers_nothing():
    node = _Node()
    assert generic.wire_device(_config(), node, None) == []
    assert node.subscribers == []


# -- the card ------------------------------------------------------------------------------


def test_the_card_is_mounted_before_the_node_that_serves_from_it(board):
    config = _config(queue_path="/sd/Outbox", device={"board": "t3s3"})
    _, notes = generic.prepare_device(config)
    assert board.mounted_at == "/sd"
    assert any("sd mounted at /sd" in note for note in notes)


def test_a_deployment_can_mount_the_card_somewhere_else(board):
    config = _config(queue_path="/data/Outbox",
                     device={"board": "t3s3", "sd_mount_point": "/data"})
    generic.prepare_device(config)
    assert board.mounted_at == "/data"


def test_a_failed_mount_stops_a_node_whose_queue_lives_on_the_card(monkeypatch):
    monkeypatch.setattr(generic, "build_board", lambda name: FakeBoard(mount_raises=True))
    config = _config(queue_path="/sd/Outbox", device={"board": "t3s3"})
    with pytest.raises(SystemExit) as e:
        generic.prepare_device(config)
    assert "/sd/Outbox" in str(e.value)


def test_a_failed_mount_stops_a_hub_whose_results_land_on_the_card(monkeypatch):
    monkeypatch.setattr(generic, "build_board", lambda name: FakeBoard(mount_raises=True))
    config = _config(node="hub", result_path="/sd/Results", device={"board": "t3s3"})
    with pytest.raises(SystemExit) as e:
        generic.prepare_device(config)
    assert "/sd/Results" in str(e.value)


def test_a_failed_mount_is_survivable_when_nothing_it_moves_lives_there(monkeypatch):
    monkeypatch.setattr(generic, "build_board", lambda name: FakeBoard(mount_raises=True))
    config = _config(queue_path="Outbox", device={"board": "t3s3"})
    _, notes = generic.prepare_device(config)
    assert any("no card" in note for note in notes)


def test_a_deployment_that_turned_the_card_off_never_mounts_it(board):
    config = _config(queue_path="Outbox", device={"board": "t3s3", "sd": False})
    generic.prepare_device(config)
    assert board.mounted_at is None


# -- the screen, the led and the log -------------------------------------------------------


def test_the_screen_is_registered_as_a_subscriber(board, fake_board_modules):
    node = _Node()
    config = _config(device={"board": "t3s3", "sd": False, "led": False})
    notes = generic.wire_device(config, node, board)
    assert "screen" in notes
    assert node.subscribers == fake_board_modules["screens"]
    assert node.notified == 1


def test_an_edge_and_a_hub_draw_different_layouts(board, fake_board_modules):
    generic.wire_device(_config(device={"board": "t3s3", "sd": False, "led": False}),
                        _Node(), board)
    generic.wire_device(_config(node="hub", device={"board": "t3s3", "sd": False, "led": False}),
                        _Node(), board)
    edge_screen, hub_screen = fake_board_modules["screens"]
    edge_keys = [row["key"] for row in edge_screen.layout_config]
    hub_keys = [row["key"] for row in hub_screen.layout_config]
    assert "SF" in edge_keys and "SF" not in hub_keys
    assert "File" in hub_keys
    # 'Signal' was the Hub's dead key: nothing has ever published it, so that row drew blank.
    assert "Signal" not in hub_keys


def test_a_missing_logo_leaves_the_readings_rather_than_refusing_to_boot(board,
                                                                        fake_board_modules):
    node = _Node()
    config = _config(device={"board": "t3s3", "sd": False, "led": False,
                             "logo_file": "there-is-no-such-file.json"})
    notes = generic.wire_device(config, node, board)
    assert any("no logo" in note for note in notes)
    assert "screen" in notes
    assert fake_board_modules["screens"][0].img_data is None


def test_a_screenless_board_with_no_screen_asked_for_wires_nothing(monkeypatch,
                                                                   fake_board_modules):
    monkeypatch.setattr(generic, "build_board", lambda name: ScreenlessBoard())
    node = _Node()
    notes = generic.wire_device(_config(device={"board": "x", "sd": False, "led": False}),
                                node, ScreenlessBoard())
    assert notes == [] and node.subscribers == []


def test_the_led_is_started_rather_than_only_built(board, fake_board_modules):
    notes = generic.wire_device(_config(device={"board": "t3s3", "sd": False, "screen": False}),
                                _Node(), board)
    assert "led" in notes
    assert fake_board_modules["leds"][0].running is True


def test_a_log_file_registers_a_logger_that_writes_where_it_was_told(board, tmp_path):
    node = _Node()
    log_file = str(tmp_path / "logs" / "log.log")
    config = _config(device={"board": "t3s3", "sd": False, "screen": False, "led": False,
                             "log_file": log_file})
    notes = generic.wire_device(config, node, board)
    assert any(log_file in note for note in notes)
    assert len(node.subscribers) == 1
    # The old logger made /sd/logs whatever it was told, so a node logging to flash still
    # needed a card and one logging elsewhere wrote into a directory that was not there.
    assert os.path.isdir(os.path.dirname(log_file))


def test_the_logger_writes_the_topics_it_was_given(tmp_path):
    from AlLoRa.Subscribers.Logger import Logger

    log_file = str(tmp_path / "logs" / "log.log")
    logger = Logger(log_file=log_file, always_log_topics=["RSSI"], change_log_topics=["SF"])
    logger.update({"RSSI": -70, "SF": 7, "Chunk": 3})
    logger.update({"RSSI": -71, "SF": 7, "Chunk": 4})
    logger.flush()

    lines = [line for line in open(log_file).read().splitlines() if line.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0].split(" - ", 3)[3])["status"]
    second = json.loads(lines[1].split(" - ", 3)[3])["status"]
    # RSSI is sampled every time; SF only when it moves, so it appears once and then stops.
    assert first == {"RSSI": -70, "SF": 7}
    assert second == {"RSSI": -71}
    # Chunk was in neither list, so it is not in the file.
    assert "Chunk" not in first


def test_the_device_section_survives_the_config_write_back(tmp_path):
    """A retune rewrites the config file. The device section has to come through it.

    The node writes back the file it read by re-reading and overlaying, so an unknown key
    survives by construction; this pins it for this key specifically, because the failure is
    quiet and slow. A node that accepts a spreading-factor change overnight and boots dark the
    next morning, with no card mounted, would look like a hardware fault.
    """
    from AlLoRa.Connectors.Loopback_connector import Loopback_connector
    from AlLoRa.Nodes.Edge import Edge

    path = str(tmp_path / "AlLoRa.json")
    device = {"board": "t3s3", "screen": False, "log_file": "/sd/logs/log.log"}
    with open(path, "w") as f:
        json.dump({
            "name": "S", "node": "edge", "protocol_version": 3, "security_mode": "open",
            "session_id": 42, "queue_path": "/sd/Outbox", "debug": False,
            "device": device,
            "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                          "tx_power": 14, "min_timeout": 0.5, "max_timeout": 12,
                          "debug": False},
        }, f)

    node = Edge(Loopback_connector("a1a1a1a1"), config_file=path)
    node.change_rf_config({"sf": 9})
    node.backup_config()

    reloaded = json.load(open(path))
    assert reloaded["device"] == device
    assert reloaded["queue_path"] == "/sd/Outbox"
    assert reloaded["connector"]["sf"] == 9
