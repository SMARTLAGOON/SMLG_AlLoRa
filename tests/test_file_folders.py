"""Unit - the folders a file being received is written under.

Every reception after the first finds its folders already there. That is the normal case, so it
says nothing; a folder that cannot be made for any other reason is still reported.
"""
import os

from AlLoRa.File import AlLoRa_File


def test_a_second_file_into_the_same_folder_prints_no_error(tmp_path, capsys):
    # Seen on the bench, 2026-10-20: every pull after the first printed
    # "Error creating base folder: [Errno 17] EEXIST", and nothing had gone wrong.
    AlLoRa_File(name="one.bin", length=1, chunk_size=10, total_len=10, path=str(tmp_path))
    capsys.readouterr()

    AlLoRa_File(name="two.bin", length=1, chunk_size=10, total_len=10, path=str(tmp_path))

    assert "Error creating" not in capsys.readouterr().out


def test_a_node_whose_results_folder_is_already_there_boots_without_an_error(tmp_path, capsys):
    # The same false alarm at boot: "Error creating result path: [Errno 17] EEXIST".
    from test_hub_endpoints import _make_hub
    (tmp_path / "results").mkdir()
    hub = _make_hub(tmp_path, nodes=[])
    hub.debug = True
    capsys.readouterr()

    hub._prepare_drive()

    assert "Error creating" not in capsys.readouterr().out


def test_a_folder_that_cannot_be_made_is_still_reported(tmp_path, capsys):
    blocked = tmp_path / "taken"
    blocked.write_text("a file where the folder should go")

    try:
        AlLoRa_File(name="x.bin", length=1, chunk_size=10, total_len=10,
                    path=str(blocked / "deeper"))
    except OSError:
        pass

    assert "Error creating base folder" in capsys.readouterr().out
    assert not os.path.isdir(str(blocked / "deeper"))
