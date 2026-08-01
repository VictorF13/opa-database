"""Unit tests for the GTFS adapter's nearest-export tie-break logic."""

import datetime

from opa_database.adapters import gtfs


def test_select_nearest_prefers_closer_date():
    """A closer candidate wins regardless of which side it's on."""
    target = datetime.date(2022, 6, 27)
    candidates = [
        (datetime.date(2022, 4, 7), "farther-before"),
        (datetime.date(2022, 8, 22), "closer-after"),
    ]
    assert gtfs._select_nearest(target, candidates) == "closer-after"


def test_select_nearest_ties_prefer_earlier():
    """Equidistant candidates resolve to the earlier one."""
    target = datetime.date(2022, 1, 15)
    candidates = [
        (datetime.date(2022, 1, 10), "before"),
        (datetime.date(2022, 1, 20), "after"),
    ]
    assert gtfs._select_nearest(target, candidates) == "before"


def test_select_nearest_single_candidate():
    """A single candidate is returned regardless of its distance."""
    target = datetime.date(2022, 1, 1)
    candidates = [(datetime.date(2022, 3, 1), "only")]
    assert gtfs._select_nearest(target, candidates) == "only"
