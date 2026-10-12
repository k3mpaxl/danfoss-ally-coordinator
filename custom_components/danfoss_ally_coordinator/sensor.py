"""Sensor entities for transparent Danfoss Ally room coordination."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DanfossAllyConfigEntry
from .discovery import ValveTarget
from .entity import DanfossAllyRoomEntity
from .house import DanfossAllyHouseEntity


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
    async_add_entities(
        LocalTemperatureGapSensor(coordinator, room, valve)
        for room in coordinator.rooms
        for valve in room.valves
        if valve.climate_entity_id and valve.calibration_entity_id
    )
    if coordinator.demand_entity_ids():
        async_add_entities(
            [
                HouseHeatDemandSensor(coordinator),
                HouseRoomsWithDemandSensor(coordinator),
            ]
        )


class RoomMeanTemperatureSensor(DanfossAllyRoomEntity, SensorEntity):
    """Median (default) or mean of the fresh temperature sources in one HA area."""

    _entity_suffix = "mean_temperature"
    _attr_translation_key = "mean_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Return unknown when sources are no longer fresh."""
        return self.coordinator.statuses[self.room.area_id].mean_temperature

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Show each valve's mode and last confirmed external-temperature write."""
        return {
            **super().extra_state_attributes,
            "method": self.coordinator.aggregation,
            "writes_resume_at": self.coordinator.writes_resume_at.isoformat()
            if self.coordinator.writes_resume_at
            else None,
            "sources": self.coordinator.source_details(self.room)["temperature"],
            "valves": self.coordinator.valve_diagnostics(self.room),
            "incomplete_valves": [
                {
                    "device_id": valve.device_id,
                    "name": valve.name,
                    "missing": list(valve.missing),
                }
                for valve in self.room.incomplete_valves
            ],
        }


class RoomTemperatureSourceCountSensor(DanfossAllyRoomEntity, SensorEntity):
    """Number of fresh numeric sources contributing to the current mean."""

    _entity_suffix = "temperature_sources"
    _attr_translation_key = "temperature_sources"
    _attr_icon = "mdi:thermometer-check"

    @property
    def native_value(self) -> int:
        """Return only sources inside the configured freshness window."""
        return self.coordinator.statuses[self.room.area_id].valid_temperature_sources


class LocalTemperatureGapSensor(DanfossAllyRoomEntity, SensorEntity):
    """Show the difference between the room mean and valve temperature."""

    _entity_suffix = "local_temperature_gap"
    _attr_translation_key = "local_temperature_gap"
    _attr_device_class = SensorDeviceClass.TEMPERATURE_DELTA
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, coordinator, room, valve: ValveTarget) -> None:
        super().__init__(coordinator, room)
        self.valve = valve
        self._attr_unique_id = f"{room.area_id}_{valve.device_id}_local_temperature_gap"
        self._attr_translation_placeholders = {
            "valve": valve.name or valve.climate_entity_id or valve.device_id
        }

    @property
    def native_value(self) -> float | None:
        """Room mean minus the valve reading; unknown unless both are fresh."""
        status = self.coordinator.statuses[self.room.area_id]
        if status.mean_temperature is None or status.evaluated_at is None:
            return None
        valve_temperature = self.coordinator.fresh_valve_temperature(
            self.valve, now=status.evaluated_at
        )
        if valve_temperature is None:
            return None
        return round(status.mean_temperature - valve_temperature, 2)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the configured offset separately from the measured gap."""
        state = self.coordinator.hass.states.get(self.valve.calibration_entity_id)
        return {
            **super().extra_state_attributes,
            "valve_climate_entity_id": self.valve.climate_entity_id,
            "offset_entity_id": self.valve.calibration_entity_id,
            "configured_offset_celsius": state.state if state else None,
            "covered_mode": self.coordinator.valve_covered(self.valve),
        }


class HouseHeatDemandSensor(DanfossAllyHouseEntity, SensorEntity):
    """Highest heat demand of any room (valve PI demand; heating floor = 100 %)."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "heat_demand")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.house_demand().max_percent


class HouseRoomsWithDemandSensor(DanfossAllyHouseEntity, SensorEntity):
    """How many rooms ask for heat right now, and which."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-thermometer"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "rooms_with_demand")

    @property
    def native_value(self) -> int | None:
        demand = self.coordinator.house_demand()
        return None if demand.any_required is None else len(demand.rooms)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"rooms": list(self.coordinator.house_demand().rooms)}
