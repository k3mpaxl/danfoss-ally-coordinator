"""Pure calculations for valve upkeep: clock, exercise plan and heat demand.

Kept free of Home Assistant objects so they can be tested exactly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

#: Zigbee Time cluster epoch: 1 January 2000, 00:00 UTC.
ZIGBEE_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# Time cluster (0x000A) attribute IDs, catalogue §1.2.
TIME = 0x0000
TIME_STATUS = 0x0001
TIME_ZONE = 0x0002
DST_START = 0x0003
DST_END = 0x0004
DST_SHIFT = 0x0005

#: Catalogue §1.2: "Write required with only bit 1 set".
TIME_STATUS_SYNCHRONIZED = 0b0000_0010

#: The SW error bit (0x0B05/0x4000 bit 10) ZHA's quirk exposes for a lost clock.
CLOCK_LOST_ERROR = "Invalid_clock_information"

EXERCISE_DAYS = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


def zigbee_seconds(moment: datetime) -> int:
    """Seconds since the Zigbee epoch."""
    return int((moment - ZIGBEE_EPOCH).total_seconds())


def _transition(tz: ZoneInfo, start: datetime, end: datetime) -> datetime:
    """Binary-search the instant between start and end where the UTC offset changes."""
    before = start.astimezone(tz).utcoffset()
    while end - start > timedelta(minutes=1):
        middle = start + (end - start) / 2
        if middle.astimezone(tz).utcoffset() == before:
            start = middle
        else:
            end = middle
    return end.replace(second=0, microsecond=0)


def time_attributes(now: datetime, tz: ZoneInfo) -> dict[int, int]:
    """The six Time cluster attributes the catalogue asks a gateway to write.

    Time zone is the standard (winter) offset east of UTC in seconds; DST start
    and end are the current year's transitions in Zigbee seconds; DST shift is
    the summer-time difference. A zone without summer time gets 0 for all three.
    """
    year = now.astimezone(tz).year
    offsets: list[tuple[datetime, timedelta]] = []
    day = datetime(year, 1, 1, 12, tzinfo=UTC)
    while day.year == year:
        offsets.append((day, day.astimezone(tz).utcoffset() or timedelta(0)))
        day += timedelta(days=1)
    standard = min(offset for _, offset in offsets)
    shift = max(offset for _, offset in offsets) - standard
    dst_start = dst_end = 0
    for (previous, before), (current, after) in zip(offsets, offsets[1:], strict=False):
        if after > before:
            dst_start = zigbee_seconds(_transition(tz, previous, current))
        elif after < before:
            dst_end = zigbee_seconds(_transition(tz, previous, current))
    return {
        TIME: zigbee_seconds(now),
        TIME_STATUS: TIME_STATUS_SYNCHRONIZED,
        TIME_ZONE: int(standard.total_seconds()),
        DST_START: dst_start,
        DST_END: dst_end,
        DST_SHIFT: int(shift.total_seconds()),
    }


def exercise_plan(
    device_ids: Iterable[str], day: str, start: time, stagger_minutes: int = 10
) -> dict[str, tuple[str, int]]:
    """Give each valve the same exercise day and a staggered minute of the day.

    Valves are ordered by device ID so the plan is stable across restarts; the
    minute wraps within the day (0..1439, the valve's own range).
    """
    first = start.hour * 60 + start.minute
    return {
        device_id: (day, (first + index * stagger_minutes) % 1440)
        for index, device_id in enumerate(sorted(device_ids))
    }


#: Where a room's demand comes from.
RADIATOR = "radiator"
FLOOR = "floor"


@dataclass(frozen=True, slots=True)
class DemandSource:
    """One room's heat demand: a Danfoss valve or a floor-heating thermostat."""

    room: str
    kind: str  # RADIATOR or FLOOR
    required: bool | None
    percent: float | None = None  # valves only (PI heating demand)


@dataclass(frozen=True, slots=True)
class RoomDemand:
    """One room that asks for heat, and through which system."""

    room: str
    kind: str
    percent: float | None


@dataclass(frozen=True, slots=True)
class HouseDemand:
    """House-wide heat demand, radiators and floor heating apart. Signal only."""

    radiators_max_percent: float | None
    radiators_required: bool | None
    floor_heating: bool | None
    any_required: bool | None
    rooms: tuple[RoomDemand, ...]

    def rooms_of(self, kind: str) -> tuple[str, ...]:
        """The names of the rooms asking for heat through one system."""
        return tuple(room.room for room in self.rooms if room.kind == kind)


def _either(*values: bool | None) -> bool | None:
    known = [value for value in values if value is not None]
    return any(known) if known else None


def house_demand(sources: Iterable[DemandSource]) -> HouseDemand:
    """Aggregate room demand per system; unknown sources do not count either way."""
    sources = tuple(sources)
    radiators = [s for s in sources if s.kind == RADIATOR]
    floors = [s for s in sources if s.kind == FLOOR]
    percents = [s.percent for s in radiators if s.percent is not None]
    known_radiators = [s for s in radiators if s.required is not None]
    known_floors = [s for s in floors if s.required is not None]
    radiators_required = (
        any(s.required for s in known_radiators) if known_radiators else None
    )
    floor_heating = any(s.required for s in known_floors) if known_floors else None

    rooms: dict[tuple[str, str], float | None] = {}
    for source in (*known_radiators, *known_floors):
        if not source.required:
            continue
        key = (source.room, source.kind)
        current = rooms.get(key)
        if source.percent is not None and (current is None or source.percent > current):
            rooms[key] = source.percent
        else:
            rooms.setdefault(key, current)
    return HouseDemand(
        radiators_max_percent=max(percents) if percents else None,
        radiators_required=radiators_required,
        floor_heating=floor_heating,
        any_required=_either(radiators_required, floor_heating),
        rooms=tuple(
            RoomDemand(room, kind, percent)
            for (room, kind), percent in sorted(rooms.items())
        ),
    )
