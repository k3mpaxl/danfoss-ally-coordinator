"""Binary sensor entities for Danfoss Ally room coordination."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DanfossAllyConfigEntry
from .entity import DanfossAllyRoomEntity
from .house import DanfossAllyHouseEntity


async def async_setup_entry(
    _hass, entry: DanfossAllyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Expose the aggregate window-open decision for every selected room."""
    coordinator = entry.runtime_data
    async_add_entities(
        RoomWindowOpenSensor(coordinator, room) for room in coordinator.rooms
    )
    if coordinator.demand_entity_ids():
        async_add_entities([HouseHeatRequiredSensor(coordinator)])


class RoomWindowOpenSensor(DanfossAllyRoomEntity, BinarySensorEntity):
    """On when any discovered opening/window/door contact in the area is open."""

    _entity_suffix = "window_open"
    _attr_translation_key = "window_open"
    _attr_device_class = BinarySensorDeviceClass.WINDOW

    @property
    def is_on(self) -> bool | None:
        """Return the same aggregate state written to eligible Danfoss eTRVs."""
        return self.coordinator.statuses[self.room.area_id].window_open

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """List every discovered contact with its state or why it is not used."""
        return {
            **super().extra_state_attributes,
            "contacts": self.coordinator.source_details(self.room)["window"],
        }


class HouseHeatRequiredSensor(DanfossAllyHouseEntity, BinarySensorEntity):
    """On while a room asks for heat (valve "heat required" or a heating floor)."""

    # No device class: "heat" would read hot/normal; the states are translated.
    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "heat_required")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.house_demand().any_required

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"rooms": list(self.coordinator.house_demand().rooms)}
