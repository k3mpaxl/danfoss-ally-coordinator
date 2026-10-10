"""Shared entity base for Danfoss Ally room coordinator diagnostics."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import RoomCoordinator
from .discovery import RoomDefinition


class DanfossAllyRoomEntity(Entity):
    """An entity backed by one computed room status, never by Zigbee polling."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Source lists and diagnostics are context, not history worth recording.
    _unrecorded_attributes = frozenset(
        {
            "area_id",
            "sources",
            "contacts",
            "valves",
            "incomplete_valves",
            "room_load_mean",
            "writes_resume_at",
            "enabled",
            "last_reason",
            "last_error",
        }
    )

    def __init__(self, coordinator: RoomCoordinator, room: RoomDefinition) -> None:
        self.coordinator = coordinator
        self.room = room
        self._attr_unique_id = f"{room.area_id}_{self._entity_suffix}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, room.area_id)},
            "name": f"Danfoss Ally – {room.name}",
            "manufacturer": "Danfoss Ally Coordinator",
            "model": "Room coordinator",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to local coordinator state changes."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Explain which room inputs drove the status without exposing raw control."""
        status = self.coordinator.statuses[self.room.area_id]
        return {
            "area_id": self.room.area_id,
            "enabled": self.coordinator.enabled,
            "last_reason": status.last_reason,
            "last_error": status.last_error,
        }
