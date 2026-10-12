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
from .maintenance import FLOOR, RADIATOR


async def async_setup_entry(
    _hass, entry: DanfossAllyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Expose the aggregate window-open decision for every selected room."""
    coordinator = entry.runtime_data
    async_add_entities(
        RoomWindowOpenSensor(coordinator, room) for room in coordinator.rooms
    )
    if coordinator.demand_entity_ids():
        house = [
            HouseHeatRequiredSensor(coordinator),
            HouseRadiatorsRequiredSensor(coordinator),
        ]
        if coordinator.demand_climates:
            house.append(HouseFloorHeatingSensor(coordinator))
        async_add_entities(house)


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
    """On while any room asks for heat - radiators or floor heating."""

    # No device class: "heat" would read hot/normal; the states are translated.
    _attr_icon = "mdi:home-thermometer"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "heat_required")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.house_demand().any_required


class HouseRadiatorsRequiredSensor(DanfossAllyHouseEntity, BinarySensorEntity):
    """On while a Danfoss valve reports heat required."""

    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "radiators_heat_required")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.house_demand().radiators_required

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"rooms": list(self.coordinator.house_demand().rooms_of(RADIATOR))}


class HouseFloorHeatingSensor(DanfossAllyHouseEntity, BinarySensorEntity):
    """On while a configured floor-heating thermostat is heating."""

    _attr_icon = "mdi:heating-coil"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "floor_heating")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.house_demand().floor_heating

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"rooms": list(self.coordinator.house_demand().rooms_of(FLOOR))}
