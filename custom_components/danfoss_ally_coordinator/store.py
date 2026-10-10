"""Durable last-success state for the Danfoss radio write cadence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from .planner import SyncRecord


class AsyncStore(Protocol):
    """The small Home Assistant Store surface used by this adapter."""

    async def async_load(self) -> dict[str, Any] | None: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


class SyncStateStore:
    """Validate and persist last successful writes independently of config entries."""

    def __init__(self, store: AsyncStore) -> None:
        self._store = store

    async def async_load(self) -> dict[str, SyncRecord]:
        """Load only finite, timezone-aware records from persistent HA storage."""
        loaded = await self._store.async_load()
        if not isinstance(loaded, Mapping) or not isinstance(
            loaded.get("records"), Mapping
        ):
            return {}
        records: dict[str, SyncRecord] = {}
        for entity_id, raw_record in loaded["records"].items():
            if not isinstance(entity_id, str) or not isinstance(raw_record, Mapping):
                continue
            try:
                value = float(raw_record["value"])
                succeeded_at = datetime.fromisoformat(str(raw_record["succeeded_at"]))
            except KeyError, TypeError, ValueError:
                continue
            if not math.isfinite(value) or succeeded_at.tzinfo is None:
                continue
            records[entity_id] = SyncRecord(value, succeeded_at)
        return records

    async def async_save(self, records: Mapping[str, SyncRecord]) -> None:
        """Persist one concise, serializable record per target Number entity."""
        await self._store.async_save(
            {
                "records": {
                    entity_id: {
                        "value": record.value,
                        "succeeded_at": record.succeeded_at.isoformat(),
                    }
                    for entity_id, record in records.items()
                }
            }
        )
