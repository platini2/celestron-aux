"""StarSense (V1) capture: each request is sent once the camera has answered the one before.

Timings and replies follow measurements on a real camera (1.02.13343): 0x90 is
answered with no data after 1-3 s, 0x94 after about 10 s with ``01 00`` (no stars)
or ``09 NN``, 0x92 at once with ``09 NN`` and then the 3C star list.
"""

import threading
import time

from celestron_aux._sample_data import SAMPLE_STAR_LIST
from celestron_aux.bus import BusEvent, Direction
from celestron_aux.catalog import Addr, Cmd
from celestron_aux.protocol import AuxPacket, CorruptFrame, FrameParser, StarSenseFrame
from celestron_aux.starsense import StarSenseCapture


class SlowCamera:
    """Answers like the camera, after a delay, on another thread (as replies arrive from the bus)."""

    def __init__(self, delays, start_reply=b"\x09\x63", silent=()):
        self.delays, self.start_reply, self.silent = delays, start_reply, set(silent)
        self.sent = []  # (time, command)
        self.capture = None

    def send(self, packet):
        self.sent.append((time.monotonic(), packet.command))
        if packet.command not in self.silent:
            reply = {Cmd.SS_RESET_CAPTURE: b"", Cmd.SS_START_CAPTURE: self.start_reply,
                     Cmd.SS_GET_CAPTURE_DATA: self.start_reply}[packet.command]
            answer = AuxPacket(Addr.STARSENSE_CAMERA, Addr.SCANNER, packet.command, reply)
            threading.Timer(self.delays.get(packet.command, 0.0), self.capture,
                            [BusEvent(0.0, Direction.RX, answer, answer.encode())]).start()
        return True


def capture_with(camera, **kwargs):
    capture = StarSenseCapture(camera.send, **kwargs)
    camera.capture = capture
    return capture


def test_each_request_waits_for_the_answer_before():
    camera = SlowCamera({Cmd.SS_RESET_CAPTURE: 0.3, Cmd.SS_START_CAPTURE: 0.6})
    assert capture_with(camera).run() == 0x63
    (t0, c0), (t1, c1), (t2, c2) = camera.sent
    assert [c0, c1, c2] == [Cmd.SS_RESET_CAPTURE, Cmd.SS_START_CAPTURE, Cmd.SS_GET_CAPTURE_DATA]
    assert t1 - t0 >= 0.3 and t2 - t1 >= 0.6  # never asks while the camera is still busy


def test_the_exposure_is_sent_as_cpwi_does():
    sent = []
    capture = StarSenseCapture(lambda packet: sent.append(packet) or False)
    capture.run()
    assert sent[0].data == bytes.fromhex("03e800")


def test_no_list_is_asked_for_when_no_stars_were_found():
    camera = SlowCamera({}, start_reply=b"\x01\x00")
    assert capture_with(camera).run() is None
    assert [c for _, c in camera.sent] == [Cmd.SS_RESET_CAPTURE, Cmd.SS_START_CAPTURE]


def test_a_camera_that_stops_answering_ends_the_capture():
    camera = SlowCamera({}, silent={Cmd.SS_START_CAPTURE})
    capture = capture_with(camera, timeouts={Cmd.SS_RESET_CAPTURE: 1, Cmd.SS_START_CAPTURE: 0.2,
                                             Cmd.SS_GET_CAPTURE_DATA: 1})
    assert capture.run() is None
    assert [c for _, c in camera.sent] == [Cmd.SS_RESET_CAPTURE, Cmd.SS_START_CAPTURE]


def test_other_traffic_is_not_taken_for_the_answer():
    capture = StarSenseCapture(lambda packet: True, timeouts={Cmd.SS_RESET_CAPTURE: 0.3})
    def chatter():
        for packet in (AuxPacket(Addr.SCANNER, Addr.STARSENSE_CAMERA, Cmd.SS_RESET_CAPTURE, b"\x03\xe8\x00"),  # our echo
                       AuxPacket(Addr.STARSENSE_CAMERA, 0x0E, Cmd.SS_RESET_CAPTURE),  # answer to the hand control
                       AuxPacket(Addr.STARSENSE_CAMERA, Addr.SCANNER, 0x91, b"\x01\x00")):
            capture(BusEvent(0.0, Direction.RX, packet, packet.encode()))
    threading.Timer(0.05, chatter).start()
    assert capture.run() is None


def test_a_star_list_one_byte_short_loses_only_itself():
    # A real camera's list once arrived with one of its 654 bytes missing.
    encoded = StarSenseFrame(SAMPLE_STAR_LIST).encode()
    short = encoded[:135] + encoded[136:]
    after = [AuxPacket(0xB4, 0x2F, 0x91, b"\x09\x63"), AuxPacket(0x10, 0x20, 0x01, b"\x01\x02\x03")]
    frames = [p.frame for p in FrameParser().feed(short + b"".join(a.encode() for a in after))]
    assert isinstance(frames[0], CorruptFrame) and frames[1:] == after
