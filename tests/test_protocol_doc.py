from pathlib import Path

from celestron_aux import protocol_doc

DOC = Path(__file__).resolve().parent.parent / "docs" / "protocol.md"


def test_committed_reference_is_up_to_date():
    # The isolated (empty) user catalog from conftest means built-in names only.
    assert DOC.read_text(encoding="utf-8") == protocol_doc.markdown(), (
        "docs/protocol.md is out of date: run "
        "celestron-aux --catalog /nonexistent.json --export-docs docs/protocol.md")


def test_reference_content():
    text = protocol_doc.markdown()
    assert "| `0xBD` | Starsense AutoAlign V2 | device |" in text
    assert "| `0x20` | CPWI | controller |" in text
    assert "| `0x01` | MTR_GET_POSITION | yes | 16/24-bit BAM position |" in text
    assert "Same commands as `0x10` AZM MC." in text
    assert "| `0x1788` | CGX |" in text


def test_your_names_are_marked(isolated_user_catalog):
    isolated_user_catalog.set_device(0x99, "My accessory")
    isolated_user_catalog.set_command(0x99, 0x10, "MY_COMMAND")
    text = protocol_doc.markdown()
    assert "| `0x99` | My accessory (your name) | device |" in text
    assert "| `0x10` | MY_COMMAND (your name) |  |  |" in text


def test_works_without_source_files(monkeypatch):
    def no_source(_):
        raise OSError("could not get source code")

    monkeypatch.setattr(protocol_doc.inspect, "getsource", no_source)
    text = protocol_doc.markdown()
    assert "| `0x01` | MTR_GET_POSITION | yes |  |" in text
