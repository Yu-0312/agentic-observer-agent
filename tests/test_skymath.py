"""Regression tests for the sky geometry the planner relies on (standard library + pytest)."""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

import pytest

import skymath as sm

PARANAL_LAT = -24.6272
PARANAL_LON = -70.4042


def test_julian_date_at_j2000():
    assert sm.julian_date(datetime(2000, 1, 1, 12, tzinfo=timezone.utc)) == pytest.approx(2451545.0)


def test_sidereal_time_at_j2000_greenwich():
    lst = sm.local_sidereal_deg(datetime(2000, 1, 1, 12, tzinfo=timezone.utc), 0.0)
    assert lst == pytest.approx(280.46061837, abs=1e-6)


def test_utc_round_trip():
    moment = sm.parse_utc("2026-10-03T23:45:10Z")
    assert sm.format_utc(moment) == "2026-10-03T23:45:10Z"


@pytest.mark.parametrize("angle,expected", [(190, -170), (-190, 170), (180, -180), (0, 0)])
def test_wrap180(angle, expected):
    assert sm.wrap180(angle) == pytest.approx(expected)


def test_radec_altaz_round_trip():
    rng = random.Random(7)
    for _ in range(200):
        ra, dec, lst = rng.uniform(0, 360), rng.uniform(-89, 89), rng.uniform(0, 360)
        alt, az = sm.radec_to_altaz(ra, dec, lst, PARANAL_LAT)
        ra2, dec2 = sm.altaz_to_radec(alt, az, lst, PARANAL_LAT)
        # separation_deg uses acos, so ~1e-6 deg is its numeric floor near zero
        assert sm.separation_deg(ra, dec, ra2, dec2) < 1e-4


def test_object_on_meridian_at_latitude_dec_is_at_zenith():
    alt, _ = sm.radec_to_altaz(120.0, PARANAL_LAT, 120.0, PARANAL_LAT)
    assert alt == pytest.approx(90.0, abs=1e-6)


def test_max_hour_angle_limits():
    # dec -80 never drops below ~14.6 deg at Paranal (circumpolar above 10 deg); dec +80 never rises.
    assert sm.max_hour_angle_deg(-80.0, PARANAL_LAT, 10.0) == 180.0
    assert 0.0 < sm.max_hour_angle_deg(-80.0, PARANAL_LAT, 20.0) < 180.0
    assert sm.max_hour_angle_deg(80.0, PARANAL_LAT, 20.0) == 0.0
    ha = sm.max_hour_angle_deg(0.0, PARANAL_LAT, 30.0)
    alt, _ = sm.radec_to_altaz(0.0, 0.0, ha, PARANAL_LAT)  # ra=0, lst=ha → hour angle = ha
    assert alt == pytest.approx(30.0, abs=1e-6)


def test_tangent_plane_shift_round_trip_including_near_zenith():
    for center_alt in (35.0, 70.0, 89.9):
        for d_north, d_east in ((0.5, -0.3), (-1.2, 0.8), (0.0, 0.0)):
            alt, az = sm.shift_altaz(center_alt, 140.0, d_north, d_east)
            offsets = sm.tangent_offsets(alt, az, center_alt, 140.0)
            assert offsets is not None
            assert offsets[0] == pytest.approx(d_north, abs=1e-9)
            assert offsets[1] == pytest.approx(d_east, abs=1e-9)


def test_tangent_offsets_behind_the_field_is_none():
    assert sm.tangent_offsets(-10.0, 0.0, 10.0, 180.0) is None


def test_airmass_is_normalised_to_one_at_zenith_and_grows_toward_horizon():
    assert sm.normalized_airmass(90.0) == pytest.approx(1.0)
    assert sm.normalized_airmass(30.0) == pytest.approx(2.0, rel=0.01)
    assert math.isinf(sm.normalized_airmass(0.0))


def test_separation_symmetry_and_poles():
    assert sm.separation_deg(10, 20, 10, 20) == pytest.approx(0.0, abs=1e-9)
    assert sm.separation_deg(0, 90, 0, -90) == pytest.approx(180.0)
    assert sm.separation_deg(15, -30, 200, 40) == pytest.approx(sm.separation_deg(200, 40, 15, -30))


def test_fiber_grid_classifies_centres_and_rejects_edges():
    grid = sm.FiberGrid({"grid_side": 4, "n_fibers": 16, "glass_side_deg": 0.2, "pitch_deg": 0.5, "fov_side_deg": 2.0})
    for fiber in range(16):
        north, east = grid.fiber_center(fiber)
        assert grid.classify(north, east) == (fiber, pytest.approx(0.1))
    fiber, margin = grid.classify(0.0, 0.0)  # between fibres: off the glass
    assert fiber is None and margin < 0
    assert grid.classify(5.0, 0.0) == (None, -1.0)


def test_planner_overrides_apply_known_keys_only(monkeypatch):
    import planner

    original = (planner.ANCHOR_POOL, planner.NEIGHBOUR_RADIUS_DEG, planner.DURATIONS)
    try:
        applied = planner.apply_overrides('{"ANCHOR_POOL": 500, "NEIGHBOUR_RADIUS_DEG": 3, "DURATIONS": [300, 600], "NOPE": 1, "ANCHORS": "x"}')
        assert applied == {"ANCHOR_POOL": 500, "NEIGHBOUR_RADIUS_DEG": 3.0, "DURATIONS": (300, 600)}
        assert planner.ANCHOR_POOL == 500 and planner.DURATIONS == (300, 600)
        assert planner.apply_overrides("not json") == {}
    finally:
        planner.ANCHOR_POOL, planner.NEIGHBOUR_RADIUS_DEG, planner.DURATIONS = original
