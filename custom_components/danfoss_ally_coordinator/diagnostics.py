"""Downloadable diagnostics for Danfoss Ally Coordinator config entries."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from . import DanfossAllyConfigEntry
from .coordinator import RoomStatus


def _status(status: RoomStatus) -> dict[str, Any]:
    data = asdict(status)
    data["evaluated_at"] = (
        status.evaluated_at.isoformat() if status.evaluated_at else None
    )
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry
) -> dict[str, Any]:
    """Return configuration, discovered rooms, computed status and sync history."""
    coordinator = entry.runtime_data
    return {
        "configuration": {**entry.data, **entry.options},
        "rooms": [
            {
                "area_id": room.area_id,
                "name": room.name,
                "temperature_sources": list(room.temperature_entity_ids),
                "window_contacts": list(room.window_entity_ids),
                "valves": [asdict(valve) for valve in room.valves],
                "sources": coordinator.source_details(room),
                "valve_status": coordinator.valve_diagnostics(room),
                "setpoint_sync": coordinator.setpoint_sync_enabled(room),
                "load_balancing": {
                    "active": coordinator.load_balancing_active(room),
                    **coordinator.load_diagnostics(room),
                },
                "status": _status(coordinator.statuses[room.area_id]),
            }
            for room in coordinator.rooms
        ],
    }
