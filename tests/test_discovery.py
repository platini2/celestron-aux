from celestron_aux.bus import BusEvent, Direction
from celestron_aux.discovery import DiscoveryTracker, is_unknown
from celestron_aux.protocol import AuxPacket


def event(packet, t=1.0):
    return BusEvent(t, Direction.RX, packet, packet.encode())


def test_known_traffic_is_not_unknown():
    assert not is_unknown(AuxPacket(0x0D, 0x10, 0x01))
    assert not is_unknown(AuxPacket(0x10, 0x0D, 0xFE, b"\x07\x0b"))


def test_unknown_device_and_unknown_command_are_flagged():
    assert is_unknown(AuxPacket(0x0D, 0x99, 0x01))          # unknown device
    assert is_unknown(AuxPacket(0x0D, 0x10, 0x77))          # known device, unknown command
    assert is_unknown(AuxPacket(0x2F, 0xB8, 0x10))          # device without a command table
    assert not is_unknown(AuxPacket(0x2F, 0xB8, 0xFE))      # ...but every device answers GET_VERSION
    assert is_unknown(AuxPacket(0x2F, 0x99, 0xFE))          # an unknown device is still flagged


def test_tracker_groups_counts_and_keeps_distinct_samples():
    tracker = DiscoveryTracker()
    tracker(event(AuxPacket(0x0D, 0x10, 0x01)))             # known: ignored
    for t, payload in enumerate([b"\x01", b"\x01", b"\x02"], start=1):
        tracker(event(AuxPacket(0x0D, 0x10, 0x77, payload), t))
    tracker(event(AuxPacket(0x10, 0x0D, 0x77, b"\xaa\xbb"), 4))
    tracker(event(AuxPacket(0x2F, 0x99, 0x05), 5))
    items = tracker.snapshot()
    assert [(d.device, d.command, d.count) for d in items] == [(0x10, 0x77, 4), (0x99, 0x05, 1)]
    first = items[0]
    assert first.requests == ["01", "02"] and first.replies == ["aabb"]
    assert first.peers == {0x0D} and (first.first_seen, first.last_seen) == (1, 4)
    assert "0x10,AZM MC,0x77,4" in tracker.to_csv()
    tracker.clear()
    assert len(tracker) == 0


def test_samples_are_capped():
    tracker = DiscoveryTracker()
    for i in range(20):
        tracker(event(AuxPacket(0x0D, 0x10, 0x77, bytes((i,)))))
    assert len(tracker.snapshot()[0].requests) == 5
