"""Pure, testable room-planning rules for Danfoss Ally coordination."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class TemperatureReading:
    """A temperature value reported by a Home Assistant source entity."""

    entity_id: str
    value: float | None
    unit: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SyncRecord:
    """The last successful external-temperature write for one eTRV."""

    value: float
    succeeded_at: datetime


# Danfoss Feature Catalogue AU417130778872en-000201, sections 2.2 and 2.4. Refresh
# deadlines leave room for the 5-minute local evaluation tick before the valve
# drops the external value (3 h exposed, 35 min covered).
EXPOSED_MIN_INTERVAL = timedelta(minutes=30)
EXPOSED_REFRESH_INTERVAL = timedelta(hours=2, minutes=45)
COVERED_MIN_INTERVAL = timedelta(minutes=5)
COVERED_REFRESH_INTERVAL = timedelta(minutes=25)
MINIMUM_CHANGE_CELSIUS = 0.1

DEFAULT_CALIBRATION_LIMIT = 2.5


def should_push_temperature(
    *,
    value: float,
    last_sync: SyncRecord | None,
    now: datetime,
    covered: bool,
) -> bool:
    """Return whether one valve needs a bounded external-temperature write."""
    if last_sync is None:
        return True
    minimum_interval = COVERED_MIN_INTERVAL if covered else EXPOSED_MIN_INTERVAL
    refresh_interval = COVERED_REFRESH_INTERVAL if covered else EXPOSED_REFRESH_INTERVAL
    elapsed = now - last_sync.succeeded_at
    # Round first: 23.2 - 23.1 is 0.0999... in binary floating point.
    changed = round(abs(value - last_sync.value), 2) >= MINIMUM_CHANGE_CELSIUS
    return elapsed >= refresh_interval or (changed and elapsed >= minimum_interval)


def _to_celsius(value: float, unit: str | None) -> float | None:
    """Convert one supported HA temperature value to Celsius."""
    if unit in ("°C", "C"):
        return value
    if unit in ("°F", "F"):
        return (value - 32.0) * 5.0 / 9.0
    return None


READING_USED = "used"
READING_UNAVAILABLE = "unavailable"
READING_STALE = "stale"
READING_UNSUPPORTED_UNIT = "unsupported_unit"


def classify_reading(
    reading: TemperatureReading, *, now: datetime, max_age: timedelta
) -> tuple[str, float | None]:
    """Return whether one reading enters the mean, and its Celsius value if so."""
    if reading.value is None or not math.isfinite(reading.value):
        return READING_UNAVAILABLE, None
    if now - reading.observed_at > max_age:
        return READING_STALE, None
    celsius = _to_celsius(reading.value, reading.unit)
    if celsius is None or not math.isfinite(celsius):
        return READING_UNSUPPORTED_UNIT, None
    return READING_USED, celsius


def _valid_celsius(
    readings: tuple[TemperatureReading, ...], *, now: datetime, max_age: timedelta
) -> list[float]:
    """Return fresh, finite, supported-unit readings converted to Celsius."""
    temperatures: list[float] = []
    for reading in readings:
        status, celsius = classify_reading(reading, now=now, max_age=max_age)
        if status == READING_USED:
            temperatures.append(celsius)
    return temperatures


def average_temperature_celsius(
    readings: tuple[TemperatureReading, ...],
    *,
    now: datetime,
    max_age: timedelta,
    method: str = "mean",
) -> float | None:
    """Return the median or mean of fresh, finite temperature readings in Celsius.

    The median ignores a single outlier, such as a sensor above a radiator.
    """
    temperatures = _valid_celsius(readings, now=now, max_age=max_age)
    if not temperatures:
        return None
    if method == "median":
        return round(statistics.median(temperatures), 2)
    return round(statistics.fmean(temperatures), 2)


def count_valid_readings(
    readings: tuple[TemperatureReading, ...],
    *,
    now: datetime,
    max_age: timedelta,
) -> int:
    """Count exactly the readings that contribute to the mean."""
    return len(_valid_celsius(readings, now=now, max_age=max_age))


def desired_local_calibration(
    *,
    room_temperature: float,
    valve_temperature: float,
    current_offset: float,
    minimum: float = -DEFAULT_CALIBRATION_LIMIT,
    maximum: float = DEFAULT_CALIBRATION_LIMIT,
) -> float:
    """Correct local measured temperature once, preserving the previous calibration.

    The valve reports its local temperature with the configured offset already
    applied, so the new offset is the old one plus the measured room gap.
    """
    values = (room_temperature, valve_temperature, current_offset, minimum, maximum)
    if not all(map(math.isfinite, values)) or minimum > maximum:
        raise ValueError("Calibration requires finite temperatures, offset and limits")
    return round(
        max(
            minimum, min(maximum, current_offset + room_temperature - valve_temperature)
        ),
        1,
    )


# Danfoss Feature Catalogue §2.2 load balancing.
LOAD_INVALID = -8000
LOAD_MINIMUM_VALID = -500
LOAD_MAX_AGE = timedelta(minutes=90)
LOAD_SEND_INTERVAL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class LoadReading:
    """One valve's own radiator load estimate (0x404A)."""

    entity_id: str
    value: float | None
    observed_at: datetime


def room_load_mean(readings: tuple[LoadReading, ...], *, now: datetime) -> int | None:
    """Average valid, recent load estimates; None if no valve reports one.

    Values below -500 (including the -8000 `invalid` sentinel) and values older
    than 90 minutes are discarded, as the catalogue requires.
    """
    loads = [
        reading.value
        for reading in readings
        if reading.value is not None
        and math.isfinite(reading.value)
        and reading.value >= LOAD_MINIMUM_VALID
        and now - reading.observed_at <= LOAD_MAX_AGE
    ]
    if not loads:
        return None
    return round(statistics.fmean(loads))
