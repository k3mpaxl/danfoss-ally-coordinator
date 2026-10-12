"""Clock, exercise and demand calculations of the valve upkeep."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.danfoss_ally_coordinator.maintenance import (
    ZIGBEE_EPOCH,
    DemandSource,
    exercise_plan,
    house_demand,
    time_attributes,
)


def test_time_attributes_follow_catalogue_1_2_for_berlin() -> None:
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    values = time_attributes(now, ZoneInfo("Europe/Berlin"))
    assert values[0] == (now - ZIGBEE_EPOCH).total_seconds()
    assert values[1] == 0b10  # only bit 1
    assert values[2] == 3600  # standard time: UTC+1, east is positive
    assert ZIGBEE_EPOCH + timedelta(seconds=values[3]) == datetime(
        2026, 3, 29, 1, 0, tzinfo=UTC
    )
    assert ZIGBEE_EPOCH + timedelta(seconds=values[4]) == datetime(
        2026, 10, 25, 1, 0, tzinfo=UTC
    )
    assert values[5] == 3600


def test_a_zone_without_summer_time_writes_no_dst() -> None:
    values = time_attributes(datetime(2026, 6, 1, tzinfo=UTC), ZoneInfo("UTC"))
    assert (values[2], values[3], values[4], values[5]) == (0, 0, 0, 0)


def test_exercise_plan_is_stable_and_staggered() -> None:
    plan = exercise_plan(["b", "a"], "Wednesday", time(10, 0))
    assert plan == {"a": ("Wednesday", 600), "b": ("Wednesday", 610)}


def test_house_demand_ignores_unknown_sources() -> None:
    assert house_demand([]).any_required is None
    demand = house_demand(
        [
            DemandSource("Büro", False, 0.0),
            DemandSource("Felix", None, None),
            DemandSource("Lukas", True, 42.0),
        ]
    )
    assert (demand.max_percent, demand.any_required, demand.rooms) == (
        42.0,
        True,
        ("Lukas",),
    )
