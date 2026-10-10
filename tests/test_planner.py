"""Behavioral tests for the Danfoss Ally room-planning rules."""

from datetime import UTC, datetime, timedelta

from custom_components.danfoss_ally_coordinator.planner import (
    SyncRecord,
    TemperatureReading,
    average_temperature_celsius,
    should_push_temperature,
)


def test_average_temperature_uses_only_fresh_numeric_temperature_sources() -> None:
    """A room average uses all valid fresh sources, converting Fahrenheit first."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    readings = (
        TemperatureReading("sensor.wall", 20.0, "°C", now - timedelta(minutes=5)),
        TemperatureReading("sensor.air", 69.8, "°F", now - timedelta(minutes=10)),
        TemperatureReading("sensor.stale", 31.0, "°C", now - timedelta(hours=3)),
        TemperatureReading("sensor.invalid", None, "°C", now - timedelta(minutes=1)),
    )

    assert (
        average_temperature_celsius(readings, now=now, max_age=timedelta(hours=2))
        == 20.5
    )


def test_temperature_sync_respects_vendor_cadence_but_refreshes_before_expiry() -> None:
    """A valve receives neither noisy writes nor an external-sensor expiry."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    last = SyncRecord(value=20.0, succeeded_at=now - timedelta(minutes=10))

    assert not should_push_temperature(
        value=20.0,
        last_sync=last,
        now=now,
        covered=False,
    )
    assert not should_push_temperature(
        value=20.2,
        last_sync=last,
        now=now,
        covered=False,
    )
    assert should_push_temperature(
        value=20.2,
        last_sync=SyncRecord(20.0, now - timedelta(minutes=30)),
        now=now,
        covered=False,
    )
    assert should_push_temperature(
        value=20.0,
        last_sync=SyncRecord(20.0, now - timedelta(hours=2, minutes=46)),
        now=now,
        covered=False,
    )
    assert should_push_temperature(
        value=20.0,
        last_sync=SyncRecord(20.0, now - timedelta(minutes=30)),
        now=now,
        covered=True,
    )
