"""Clock, exercise and demand calculations of the valve upkeep."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.danfoss_ally_coordinator.maintenance import (
    FLOOR,
    RADIATOR,
    ZIGBEE_EPOCH,
    DemandSource,
    RoomDemand,
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


def test_house_demand_keeps_radiators_and_floor_heating_apart() -> None:
    assert house_demand([]).any_required is None
    demand = house_demand(
        [
            DemandSource("Büro", RADIATOR, False, 0.0),
            DemandSource("Felix", RADIATOR, None, None),
            DemandSource("Lukas", RADIATOR, True, 42.0),
            DemandSource("Lukas", RADIATOR, True, 30.0),
            DemandSource("Bad", FLOOR, True),
            DemandSource("WC", FLOOR, False),
        ]
    )
    assert demand.radiators_max_percent == 42.0  # floor heating never adds a %
    assert demand.radiators_required is True and demand.floor_heating is True
    assert demand.any_required is True
    assert demand.rooms == (
        RoomDemand("Bad", FLOOR, None),
        RoomDemand("Lukas", RADIATOR, 42.0),
    )
    assert demand.rooms_of(FLOOR) == ("Bad",)


def test_floor_heating_alone_makes_a_room_need_heat() -> None:
    demand = house_demand(
        [DemandSource("Büro", RADIATOR, False, 5.0), DemandSource("Bad", FLOOR, True)]
    )
    assert demand.radiators_required is False and demand.any_required is True
