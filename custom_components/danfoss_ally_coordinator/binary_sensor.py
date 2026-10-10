"""Binary sensor entities for Danfoss Ally room coordination."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DanfossAllyConfigEntry
from .entity import DanfossAllyRoomEntity


async def async_setup_entry(
    _hass, entry: DanfossAllyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Expose the aggregate window-open decision for every selected room."""
    coordinator = entry.runtime_data
    async_add_entities(
        RoomWindowOpenSensor(coordinator, room) for room in coordinator.rooms
    )


class RoomWindowOpenSensor(DanfossAllyRoomEntity, BinarySensorEntity):
    """On when any discovered opening/window/door contact in the area is open."""

    _entity_suffix = "window_open"
    _attr_translation_key = "window_open"
    _attr_device_class = BinarySensorDeviceClass.WINDOW

    @property
    def is_on(self) -> bool:
        """Return the same aggregate state written to eligible Danfoss eTRVs."""
        return self.coordinator.statuses[self.room.area_id].window_open
