"""The Earth and sky spheres from real traffic: location, motors and plates, no emulator."""

import math
import random
import struct
from datetime import datetime, timezone

import pytest

from celestron_aux.bus import BusEvent, Direction
from celestron_aux.protocol import AuxPacket, StarSenseFrame
from celestron_aux.protocol.units import degrees_to_bam
from celestron_aux.sky import altaz_to_j2000, j2000_to_altaz
from celestron_aux.sky_observer import EPOCH_2000, MountModel, SkyObserver, _separation, fit_mount
from celestron_aux.star_solver import list_id

T0 = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc).timestamp()
SITE = (-34.6, -58.4)
APP, AZM, ALT, GPS, V1, V2 = 0x20, 0x10, 0x11, 0xB0, 0xB4, 0xBD


def test_j2000_and_alt_az_convert_both_ways():
    when = datetime.fromtimestamp(T0, timezone.utc)
    for alt, az in [(40, 120), (10, 350), (85, 10), (-5, 200)]:
        ra, dec = altaz_to_j2000(alt, az, *SITE, when)
        assert j2000_to_altaz(ra, dec, *SITE, when) == pytest.approx((alt, az), abs=1e-6)


def _samples(truth, count, rng):
    samples = []
    for _ in range(count):
        azm, alt = rng.uniform(0, 360), rng.uniform(5, 70)
        sky_alt, sky_az = truth.pointing(azm, alt)
        samples.append((azm, alt, sky_alt, sky_az))
    return samples


def _worst(model, truth, rng):
    return max(_separation(model.pointing(azm, alt), truth.pointing(azm, alt))
               for azm, alt in [(rng.uniform(0, 360), rng.uniform(5, 70)) for _ in range(40)])


@pytest.mark.parametrize("truth, plates", [
    (MountModel(90, 0, 37.0, -3.0, 1, 1, 0, 0), 1),       # level alt-az: one plate is enough
    (MountModel(90, 0, 200.0, 5.0, -1, 1, 0, 0), 2),      # the azimuth motor turning the other way: two
    (MountModel(34.6, 180, 80.0, -10.0, 1, 1, 0, 0), 3),  # a wedge in the south: three
    (MountModel(87.5, 40, 10.0, 2.0, 1, 1, 0, 0), 3),     # a mount 2.5 degrees off level
])
def test_the_mount_is_fitted_from_its_plates(truth, plates):
    rng = random.Random(7)
    model = fit_mount(_samples(truth, plates, rng))
    assert model.rms < 0.05 and _worst(model, truth, rng) < 0.1
    assert model.kind == ("alt-az" if truth.inclination == 90 else "tilted")


def test_two_plates_do_not_claim_a_tilt():
    # Two plates fit any tilted model exactly, so they prove nothing: the level fit stays, with its error.
    rng = random.Random(3)
    truth = MountModel(34.6, 180, 80.0, -10.0, 1, 1, 0, 0)
    model = fit_mount(_samples(truth, 2, rng))
    assert model.kind == "alt-az" and model.rms > 1


class FakeSolver:
    def __init__(self):
        self.results = {}

    def result(self, key):
        return self.results.get(key)


def _star_list(seed):
    return StarSenseFrame(bytes(random.Random(seed).getrandbits(8) for _ in range(800)))


class Feed:
    """Real traffic as the bus would deliver it, ``now`` seconds into the session."""

    def __init__(self, emulated=()):
        self.solver = FakeSolver()
        self.observer = SkyObserver(self.solver, lambda address: address in emulated, lambda stamp: T0 + stamp)
        self.now = 0.0

    def packet(self, source, destination, command, data=b""):
        self.observer(BusEvent(self.now, Direction.RX, AuxPacket(source, destination, command, data), b""))

    def frame(self, frame):
        self.observer(BusEvent(self.now, Direction.RX, frame, b""))

    def gps(self, latitude, longitude):
        self.packet(GPS, APP, 0x01, degrees_to_bam(latitude))
        self.packet(GPS, APP, 0x02, degrees_to_bam(longitude))

    def motors(self, azm, alt):
        for address, degrees in ((AZM, azm), (ALT, alt)):
            self.packet(address, APP, 0x01, (round(degrees / 360 * (1 << 24)) % (1 << 24)).to_bytes(3, "big"))

    def capture(self, seed, truth, azm, alt):
        """A V1 capture with the motors at azm/alt; the solver finds where ``truth`` says it was."""
        self.motors(azm, alt)
        self.packet(APP, V1, 0x94, b"\x00")
        self.now += 11
        frame = _star_list(seed)
        self.frame(frame)
        sky_alt, sky_az = truth.pointing(azm, alt)
        ra, dec = altaz_to_j2000(sky_alt, sky_az, *SITE, datetime.fromtimestamp(T0 + self.now - 11, timezone.utc))
        self.solver.results[list_id(frame.payload)] = {"status": "solved", "ra": ra, "dec": dec}


def test_real_traffic_builds_the_spheres():
    truth = MountModel(90, 0, 140.0, 2.0, 1, 1, 0, 0)
    feed = Feed()
    assert not feed.observer.has_data()
    feed.gps(*SITE)
    feed.motors(10, 30)
    view = feed.observer.view()
    assert view["site"]["from"] == "GPS" and view["site"]["lat"] == pytest.approx(SITE[0], abs=1e-4)
    assert view["pointing"] is None and view["motors"]  # motors seen, but no plate yet to place them
    feed.capture(1, truth, 10, 30)
    feed.now += 5
    feed.motors(60, 45)  # the telescope moves on
    view = feed.observer.view()
    assert view["model"]["kind"] == "alt-az" and view["model"]["plates"] == 1
    assert [p["kind"] for p in view["plates"]] == ["V1 capture"] and view["plates"][0]["sky"]
    expected = truth.pointing(60, 45)
    assert (view["pointing"]["alt"], view["pointing"]["az"]) == pytest.approx(expected, abs=0.01)
    ra, dec = altaz_to_j2000(*expected, *SITE, datetime.fromtimestamp(T0 + feed.now, timezone.utc))
    assert view["trail"][-1] == pytest.approx([ra, dec], abs=0.01)
    assert len(view["trail"]) >= 2  # each motor answers on its own, so a move can add a step
    feed.observer.clear_path()
    assert len(feed.observer.view()["trail"]) == 1


def test_the_same_star_list_twice_is_one_plate():
    truth = MountModel(90, 0, 0.0, 0.0, 1, 1, 0, 0)
    feed = Feed()
    feed.gps(*SITE)
    feed.capture(1, truth, 10, 30)
    feed.frame(_star_list(1))  # asked for again
    assert len(feed.observer.view()["plates"]) == 1


def test_a_star_list_still_solving_is_listed_without_a_place():
    feed = Feed()
    feed.packet(APP, V1, 0x94, b"\x00")
    feed.frame(_star_list(2))
    feed.solver.results[list_id(_star_list(2).payload)] = {"status": "solving"}
    view = feed.observer.view()
    assert view["plates"][0]["sky"] is None and view["plates"][0]["status"] == "solving"
    assert view["model"] is None and view["site"] is None


def test_what_emulators_say_on_the_bus_counts_and_is_labelled():
    feed = Feed(emulated={AZM, ALT, GPS, V1, V2})
    feed.gps(*SITE)
    feed.motors(10, 30)
    feed.frame(_star_list(3))
    feed.packet(V2, APP, 0x02, bytes((1,)) + struct.pack(">ff", 1.0, -0.5))
    view = feed.observer.view()
    assert view["site"]["from"] == "GPS emulator" and view["motors"]
    assert [p["emulated"] for p in view["plates"]] == [True, True]
    assert view["emulated"] == ["telescope", "GPS", "StarSense V1", "StarSense V2"]
    assert Feed().observer.view()["emulated"] == []


def test_the_location_cpwi_sends_is_used_without_a_gps():
    feed = Feed(emulated={V2})
    feed.packet(APP, V2, 0x31, struct.pack(">ff", *SITE))  # what CPWI says is real, whoever it tells
    assert feed.observer.view()["site"] == {"lat": pytest.approx(SITE[0]), "lon": pytest.approx(SITE[1]), "from": "CPWI"}


def test_a_real_starsense_v2_solve_is_a_plate_once():
    feed = Feed()
    feed.packet(APP, V2, 0x31, struct.pack(">ff", *SITE))
    feed.motors(100, 40)
    feed.packet(APP, V2, 0x01)
    reply = bytes((1,)) + struct.pack(">ff", math.radians(120.0), math.radians(-40.0))
    for _ in range(3):  # CPWI keeps asking once it is solved
        feed.now += 1
        feed.packet(V2, APP, 0x02, reply)
    plates = feed.observer.view()["plates"]
    assert len(plates) == 1 and plates[0]["kind"] == "V2 solve"
    assert plates[0]["sky"] == {"ra": pytest.approx(120.0), "dec": pytest.approx(-40.0)}


def test_a_replay_takes_the_real_time_from_cpwi():
    # Replayed at another time: CPWI telling the camera the time puts the plates back when they were.
    feed = Feed()
    recorded = datetime(2025, 3, 1, 2, 0, tzinfo=timezone.utc).timestamp()
    feed.packet(APP, V2, 0x32, int(recorded - EPOCH_2000).to_bytes(4, "big"))
    feed.packet(APP, V1, 0x94, b"\x00")
    feed.frame(_star_list(4))
    assert feed.observer.view()["plates"][0]["time"].startswith("2025-03-01T02:00")


def test_a_motor_just_below_zero_is_not_a_full_turn_away():
    # The altitude motor a little below its zero reads nearly 360 degrees.
    truth = MountModel(90, 0, 120.0, 0.0, 1, 1, 0, 0)
    samples = [(azm, alt % 360, *truth.pointing(azm, alt)) for azm, alt in [(0, 5), (90, 60), (300, -11)]]
    model = fit_mount(samples)
    assert model.kind == "alt-az" and model.rms < 0.01


def test_a_telescope_past_the_zenith_is_the_same_mount():
    # Past the zenith the altitude motor reads over 90: the sky sees it the other way round.
    truth = MountModel(90, 0, 120.0, 40.0, 1, 1, 0, 0)
    samples = [(azm, alt, *truth.pointing(azm, alt)) for azm, alt in [(0, 0), (85.7, 0), (85.7, 64.5), (312, 64.5)]]
    model = fit_mount(samples)
    assert model.kind == "alt-az" and model.rms < 0.01
