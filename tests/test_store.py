"""Persistent last-success state prevents unnecessary writes after HA restart."""

import asyncio
from datetime import UTC, datetime

from custom_components.danfoss_ally_coordinator.planner import SyncRecord
from custom_components.danfoss_ally_coordinator.store import SyncStateStore


class FakeStore:
    """Minimal async Store substitute for behavioral testing."""

    def __init__(self, data: dict[str, object] | None = None) -> None:
        self.data = data
        self.saved: dict[str, object] | None = None

    async def async_load(self) -> dict[str, object] | None:
        return self.data

    async def async_save(self, data: dict[str, object]) -> None:
        self.saved = data


def test_sync_state_store_round_trips_only_valid_records() -> None:
    """Malformed historical data cannot block a fresh temperature synchronization."""
    store = FakeStore(
        {
            "records": {
                "number.good": {
                    "value": 20.5,
                    "succeeded_at": "2026-10-09T11:30:00+00:00",
                },
                "number.broken": {"value": "not-a-number", "succeeded_at": "never"},
            }
        }
    )
    state_store = SyncStateStore(store)

    restored = asyncio.run(state_store.async_load())
    assert restored == {
        "number.good": SyncRecord(20.5, datetime(2026, 10, 9, 11, 30, tzinfo=UTC))
    }

    asyncio.run(
        state_store.async_save(
            {"number.good": SyncRecord(21.0, datetime(2026, 10, 9, 12, 0, tzinfo=UTC))}
        )
    )
    assert store.saved == {
        "records": {
            "number.good": {"value": 21.0, "succeeded_at": "2026-10-09T12:00:00+00:00"}
        }
    }
