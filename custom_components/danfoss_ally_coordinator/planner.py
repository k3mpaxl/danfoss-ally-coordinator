"""Pure, testable room-planning rules for Danfoss Ally coordination."""

from __future__ import annotations

import math
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


EXPOSED_MIN_INTERVAL = timedelta(minutes=30)
EXPOSED_REFRESH_INTERVAL = timedelta(hours=2, minutes=45)
COVERED_MIN_INTERVAL = timedelta(minutes=5)
COVERED_REFRESH_INTERVAL = timedelta(minutes=30)
MINIMUM_CHANGE_CELSIUS = 0.1


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
    changed = abs(value - last_sync.value) >= MINIMUM_CHANGE_CELSIUS
    return elapsed >= refresh_interval or (changed and elapsed >= minimum_interval)


def _to_celsius(value: float, unit: str | None) -> float | None:
    """Convert one supported HA temperature value to Celsius."""
    if unit in ("°C", "C"):
        return value
    if unit in ("°F", "F"):
        return (value - 32.0) * 5.0 / 9.0
    return None


def average_temperature_celsius(
    readings: tuple[TemperatureReading, ...],
    *,
    now: datetime,
    max_age: timedelta,
) -> float | None:
    """Return the arithmetic mean of fresh, finite temperature readings in Celsius."""
    temperatures: list[float] = []
    for reading in readings:
        if reading.value is None or now - reading.observed_at > max_age:
            continue
        celsius = _to_celsius(reading.value, reading.unit)
        if celsius is not None and math.isfinite(celsius):
            temperatures.append(celsius)
    if not temperatures:
        return None
    return round(sum(temperatures) / len(temperatures), 2)
