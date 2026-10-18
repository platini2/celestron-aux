import io

from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.protocol import AuxPacket
from celestron_aux.transport.playback import PlaybackTransport, load_capture

import pytest

from .fakes import FakeMount, wait_until


def test_a_mark_reaches_every_log(tmp_path):
    out = io.StringIO()
    config = AppConfig(save_starsense_images=False, log_csv=True, record_raw=True,
                       csv_path=tmp_path / "log.csv", raw_path=tmp_path / "raw.txt")
    app = ScannerApp(FakeMount({}), config, stream=out)
    app.start(initial_scan=False)
    try:
        assert app.mark("  pressed   Align\non the HC ") == "pressed Align on the HC"
        assert wait_until(lambda: "MARK" in (tmp_path / "log.csv").read_text())
        with pytest.raises(ValueError):
            app.mark("   ")
    finally:
        app.close()
    assert "MARK: pressed Align on the HC" in out.getvalue()
    assert ",MARK,,pressed Align on the HC," in (tmp_path / "log.csv").read_text()
    raw = (tmp_path / "raw.txt").read_text().splitlines()
    assert any(line.startswith("#MARK ") and line.endswith(" pressed Align on the HC") for line in raw)


def test_a_replay_brings_marks_back_at_their_recorded_time(tmp_path):
    capture = tmp_path / "capture.txt"
    packet = AuxPacket(0x10, 0x2F, 0xFE, b"\x07\x0b").encode().hex()
    capture.write_text(f"# a comment\n0.0 {packet}\n#MARK 0.5 pressed Align\n1.0 {packet}\n")
    out = io.StringIO()
    app = ScannerApp(PlaybackTransport(capture, speed=100), AppConfig(save_starsense_images=False), stream=out)
    app.start(initial_scan=False)
    try:
        assert wait_until(lambda: "MARK" in out.getvalue())
    finally:
        app.close()
    assert "      0.500000 - MARK: pressed Align" in out.getvalue()


def test_a_mark_made_during_a_replay_uses_the_replayed_time(tmp_path):
    capture = tmp_path / "capture.txt"
    packet = AuxPacket(0x10, 0x2F, 0xFE, b"\x07\x0b").encode().hex()
    capture.write_text("".join(f"{t} {packet}\n" for t in (100.0, 130.0, 160.0)))
    out = io.StringIO()
    app = ScannerApp(PlaybackTransport(capture, speed=60), AppConfig(save_starsense_images=False), stream=out)
    app.start(initial_scan=False)
    try:
        assert wait_until(lambda: out.getvalue().count("MTR_GET_VERSION") >= 2)  # 30 s into the recording
        app.mark("here")
        assert wait_until(lambda: "MARK" in out.getvalue())
    finally:
        app.close()
    line = next(line for line in out.getvalue().splitlines() if "MARK: here" in line)
    assert float(line.split(" - ")[0]) >= 30.0  # recording time, not the second or so since the replay began


def test_malformed_mark_lines_are_reported(tmp_path):
    capture = tmp_path / "capture.txt"
    capture.write_text("#MARK not-a-time hello\n")
    with pytest.raises(ValueError, match="#MARK"):
        load_capture(capture)
