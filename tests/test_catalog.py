import json

import pytest

from celestron_aux import catalog
from celestron_aux.bus import BusEvent, Direction
from celestron_aux.decoding import describe_packet
from celestron_aux.discovery import DiscoveryTracker
from celestron_aux.protocol import AuxPacket


def test_your_names_layer_over_the_built_in_ones(isolated_user_catalog):
    user = isolated_user_catalog
    assert catalog.device_name(0x99) == "Unknown" and not catalog.is_known_device(0x99)
    user.set_device(0x99, "Polar finder")
    user.set_command(0x10, 0x77, "MTR_GET_TEMPERATURE")
    user.set_command(0x10, 0x01, "MY_POSITION")  # yours wins over the built-in name
    assert catalog.device_name(0x99) == "Polar finder" and catalog.is_known_device(0x99)
    assert catalog.command_name(0x10, 0x77) == "MTR_GET_TEMPERATURE"
    assert catalog.command_name(0x10, 0x01) == "MY_POSITION"
    assert (0x77, "MTR_GET_TEMPERATURE") in catalog.commands_for(0x10)
    assert 0x99 in catalog.known_addresses()
    assert describe_packet(AuxPacket(0x0D, 0x10, 0x77)).command == "MTR_GET_TEMPERATURE"
    user.set_command(0x10, 0x01, None)  # forgetting it brings the built-in name back
    assert catalog.command_name(0x10, 0x01) == "MTR_GET_POSITION"


def test_names_are_saved_and_loaded(isolated_user_catalog, tmp_path):
    user = isolated_user_catalog
    user.set_device(0x99, "  Polar   finder ")  # spacing is tidied
    user.set_command(0x99, 0x05, "PF_GET_STATE")
    user.save()
    saved = json.loads(user.path.read_text())
    assert saved == {"devices": {"0x99": "Polar finder"}, "commands": {"0x99": {"0x05": "PF_GET_STATE"}}}
    again = catalog.UserCatalog(user.path)
    again.load()
    assert again.entries() == [
        {"device": 0x99, "command": None, "name": "Polar finder"},
        {"device": 0x99, "command": 0x05, "name": "PF_GET_STATE"},
    ]
    assert not list(tmp_path.glob(".catalog-*"))  # no temporary files left behind


def test_a_broken_file_is_reported_and_never_overwritten(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text("{ this is not json")
    user = catalog.UserCatalog(path)
    user.load()
    assert user.load_error and "catalog.json" in user.load_error
    user.set_device(0x99, "Anything")
    with pytest.raises(catalog.CatalogError):
        user.save()
    assert path.read_text() == "{ this is not json"


@pytest.mark.parametrize("name", ["", "   ", "x" * 41, "bad\x00name"])
def test_unacceptable_names_are_refused(isolated_user_catalog, name):
    with pytest.raises(catalog.CatalogError):
        isolated_user_catalog.set_device(0x99, name)


def test_naming_clears_the_matching_unknown_traffic(isolated_user_catalog):
    tracker = DiscoveryTracker()
    for packet in (AuxPacket(0x0D, 0x10, 0x77, b"\x01"), AuxPacket(0x0D, 0x99, 0x05)):
        tracker(BusEvent(1.0, Direction.RX, packet, packet.encode()))
    assert len(tracker) == 2
    isolated_user_catalog.set_command(0x10, 0x77, "MTR_GET_TEMPERATURE")
    assert tracker.prune() == 1
    assert [(d.device, d.command) for d in tracker.snapshot()] == [(0x99, 0x05)]
