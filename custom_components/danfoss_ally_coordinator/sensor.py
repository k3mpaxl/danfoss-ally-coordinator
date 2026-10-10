"""Sensor entities for transparent Danfoss Ally room coordination."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DanfossAllyConfigEntry
from .entity import DanfossAllyRoomEntity


async def async_setup_entry(
    _hass, entry: DanfossAllyConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Expose computed mean and source count for every selected room."""
    coordinator = entry.runtime_data
    async_add_entities(
        entity
        for room in coordinator.rooms
        for entity in (
            RoomMeanTemperatureSensor(coordinator, room),
            RoomTemperatureSourceCountSensor(coordinator, room),
        )
    )


class RoomMeanTemperatureSensor(DanfossAllyRoomEntity, SensorEntity):
    """Arithmetic mean over fresh detected temperature sources in one HA area."""

    _entity_suffix = "mean_temperature"
    _attr_translation_key = "mean_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return unknown when sources are no longer fresh."""
        return self.coordinator.statuses[self.room.area_id].mean_temperature


class RoomTemperatureSourceCountSensor(DanfossAllyRoomEntity, SensorEntity):
    """Number of fresh numeric sources contributing to the current mean."""

    _entity_suffix = "temperature_sources"
    _attr_translation_key = "temperature_sources"
    _attr_icon = "mdi:thermometer-check"

    @property
    def native_value(self) -> int:
        """Return only sources inside the configured freshness window."""
        return self.coordinator.statuses[self.room.area_id].valid_temperature_sources
