"""ZHA v2 overlay for Danfoss eTRV0103 (Danfoss Ally).

The appliance's ordinary Climate entity uses the upstream fast/user setpoint
command. This overlay adds a separate scheduled setpoint input that writes the
standard OccupiedHeatingSetpoint attribute with Danfoss manufacturer code,
without sending the fast command.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from zha.quirks import DANFOSS_ALLY_THERMOSTAT
from zhaquirks.builder import EntityType, QuirkBuilder, UnitOfTemperature
from zhaquirks.danfoss.thermostat import (
    DanfossDiagnosticCluster,
    DanfossThermostatCluster,
    DanfossTimeCluster,
    DanfossUserInterfaceCluster,
)
from zigpy import types
from zigpy.zcl.foundation import ZCLAttributeDef

THERMOSTAT_CLUSTER_ID = 0x0201
DIAGNOSTIC_CLUSTER_ID = 0x0B05
DANFOSS_MANUFACTURER_CODE = 0x1246
SCHEDULED_SETPOINT_ALIAS_ID = 0xFFF0  # local-only alias; intercepted below


class DanfossProgrammingOperationMode(IntEnum):
    """Values used by Zigbee2MQTT's Danfoss Ally converter."""

    setpoint = 0
    schedule = 1
    schedule_with_preheat = 3
    eco = 4


_STATUS_BITS = (
    (0x0001, "top_pcb_sensor_error"),
    (0x0002, "side_pcb_sensor_error"),
    (0x0004, "non_volatile_memory_error"),
    (0x0008, "unknown_hw_error"),
    (0x0020, "motor_error"),
    (0x0080, "invalid_internal_communication"),
    (0x0200, "invalid_clock_information"),
    (0x0800, "radio_communication_error"),
    (0x1000, "encoder_jammed"),
    (0x2000, "low_battery"),
    (0x4000, "critical_low_battery"),
)


def decode_system_status(code: int) -> str:
    """Render Danfoss' diagnostic bitmap as Zigbee2MQTT-style text."""
    errors = [name for bit, name in _STATUS_BITS if int(code) & bit]
    return ",".join(errors) if errors else "ok"


def zcl_setpoint(celsius: float) -> int:
    """Convert a valid 0.5 C Danfoss setpoint to ZCL hundredths of a degree."""
    value = float(celsius)
    if not 5 <= value <= 35 or round(value * 2) != value * 2:
        raise ValueError("Danfoss setpoint must be 5..35 °C in 0.5 °C steps")
    return round(value * 100)


def format_exercise_time(minutes: int) -> str:
    """Render Danfoss' minutes-since-midnight exercise time as a wall-clock time."""
    value = int(minutes)
    if not 0 <= value <= 1439:
        return "Nicht verfügbar"
    return f"{value // 60:02d}:{value % 60:02d}"


def describe_room_load(value: int) -> str:
    """Explain the Danfoss room-load control value instead of exposing its sentinel."""
    raw_value = int(value)
    if raw_value == -8000:
        return "Nicht verfügbar (Lastverteilung inaktiv)"
    if raw_value < -500:
        return f"Ungültig ({raw_value})"
    return f"Aktiv (Steuerwert: {raw_value})"


def describe_external_room_sensor(value: int) -> str:
    """Render the Danfoss external-room-sensor sentinel as an actual state."""
    raw_value = int(value)
    if raw_value == -8000:
        return "Nicht verbunden / deaktiviert"
    return f"{raw_value / 100:.1f} °C"


class DanfossAllyOptimizedThermostatCluster(DanfossThermostatCluster):
    """Adds an intercepted alias for scheduled (slow) occupied setpoints."""

    class AttributeDefs(DanfossThermostatCluster.AttributeDefs):
        scheduled_setpoint_input = ZCLAttributeDef(
            id=SCHEDULED_SETPOINT_ALIAS_ID,
            type=types.int16s,
            access="rw",
            is_manufacturer_specific=False,
        )

    def _update_attribute(self, attrid: int, value: Any) -> None:
        super()._update_attribute(attrid, value)
        if attrid == self.AttributeDefs.occupied_heating_setpoint.id:
            # Let the second Number entity track the same reported setpoint.
            super()._update_attribute(SCHEDULED_SETPOINT_ALIAS_ID, value)

    async def read_attributes(self, attributes, *args, **kwargs):
        """Alias reads to the real ZCL attribute; never read the local fake ID."""
        alias = self.AttributeDefs.scheduled_setpoint_input
        requested = list(attributes)
        if not any(
            item == alias.id or item == alias.name or item is alias
            for item in requested
        ):
            return await super().read_attributes(requested, *args, **kwargs)

        mapped = [
            self.AttributeDefs.occupied_heating_setpoint.name
            if item == alias.id or item == alias.name or item is alias
            else item
            for item in requested
        ]
        success, failure = await super().read_attributes(mapped, *args, **kwargs)
        if self.AttributeDefs.occupied_heating_setpoint.name in success:
            value = success.pop(self.AttributeDefs.occupied_heating_setpoint.name)
            success[alias.name] = value
            super()._update_attribute(alias.id, value)
        if self.AttributeDefs.occupied_heating_setpoint.name in failure:
            failure[alias.name] = failure.pop(
                self.AttributeDefs.occupied_heating_setpoint.name
            )
        return success, failure

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Scheduled alias writes slow setpoint; normal entity retains fast writes."""
        alias = self.AttributeDefs.scheduled_setpoint_input
        alias_keys = [
            key
            for key in attributes
            if key == alias.id or key == alias.name or key is alias
        ]
        if not alias_keys:
            return await super().write_attributes(
                attributes, manufacturer=manufacturer, **kwargs
            )
        if len(attributes) != 1 or len(alias_keys) != 1:
            raise ValueError("Scheduled setpoint alias must be written by itself")

        raw_value = int(attributes[alias_keys[0]])
        if not 500 <= raw_value <= 3500 or raw_value % 50:
            raise ValueError("Danfoss setpoint must be 5..35 °C in 0.5 °C steps")

        # Bypass DanfossThermostatCluster.write_attributes, which deliberately
        # sends User_interaction (the fast command) for ordinary setpoint writes.
        result = await super(DanfossThermostatCluster, self).write_attributes(
            {"occupied_heating_setpoint": raw_value},
            manufacturer=DANFOSS_MANUFACTURER_CODE,
            **kwargs,
        )
        if all(record.status == 0 for group in result for record in group):
            super()._update_attribute(alias.id, raw_value)
        return result


QUIRK_ENTRY = (
    QuirkBuilder("Danfoss", "eTRV0103")
    .replaces(DanfossAllyOptimizedThermostatCluster)
    .replaces(DanfossUserInterfaceCluster)
    .replaces(DanfossDiagnosticCluster)
    .replaces(DanfossTimeCluster)
    .exposes_feature(DANFOSS_ALLY_THERMOSTAT)
    # Keep the writable raw controls for advanced use, but name them accurately.
    .change_entity_metadata(
        cluster_id=THERMOSTAT_CLUSTER_ID,
        unique_id_suffix="exercise_trigger_time",
        new_translation_key="danfoss_exercise_time_raw",
        new_fallback_name="Ventilübungszeit (Minuten nach Mitternacht)",
    )
    .change_entity_metadata(
        cluster_id=THERMOSTAT_CLUSTER_ID,
        unique_id_suffix="load_room_mean",
        new_translation_key="danfoss_room_load_raw",
        new_fallback_name="Raumlast-Mittelwert (Gateway-Steuerwert)",
    )
    .change_entity_metadata(
        cluster_id=THERMOSTAT_CLUSTER_ID,
        unique_id_suffix="external_measured_room_sensor",
        new_translation_key="external_room_sensor_raw",
        new_fallback_name="Externes Raumtemperatur-Signal (−80 °C = deaktiviert)",
    )
    .sensor(
        attribute_name="external_measured_room_sensor",
        cluster_id=THERMOSTAT_CLUSTER_ID,
        attribute_converter=describe_external_room_sensor,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="external_room_sensor_status",
        unique_id_suffix="external_room_sensor_status",
        fallback_name="Status externes Raumthermometer",
    )
    .sensor(
        attribute_name="exercise_trigger_time",
        cluster_id=THERMOSTAT_CLUSTER_ID,
        attribute_converter=format_exercise_time,
        entity_type=EntityType.CONFIG,
        translation_key="exercise_time",
        unique_id_suffix="exercise_time",
        fallback_name="Ventilübungszeit",
    )
    .sensor(
        attribute_name="load_room_mean",
        cluster_id=THERMOSTAT_CLUSTER_ID,
        attribute_converter=describe_room_load,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="room_load_status",
        unique_id_suffix="room_load_status",
        fallback_name="Status der Raumlastverteilung",
    )
    .enum(
        attribute_name="programing_oper_mode",
        enum_class=DanfossProgrammingOperationMode,
        cluster_id=THERMOSTAT_CLUSTER_ID,
        attribute_initialized_from_cache=False,
        translation_key="programming_operation_mode",
        unique_id_suffix="programming_operation_mode",
        fallback_name="Programming operation mode",
    )
    .number(
        attribute_name="scheduled_setpoint_input",
        cluster_id=THERMOSTAT_CLUSTER_ID,
        min_value=5,
        max_value=35,
        step=0.5,
        unit=UnitOfTemperature.CELSIUS,
        multiplier=0.01,
        mode="slider",
        attribute_initialized_from_cache=False,
        translation_key="occupied_heating_setpoint_scheduled",
        unique_id_suffix="occupied_heating_setpoint_scheduled",
        fallback_name="Scheduled heating setpoint",
    )
    .sensor(
        attribute_name="abs_max_heat_setpoint_limit",
        cluster_id=THERMOSTAT_CLUSTER_ID,
        multiplier=0.01,
        unit=UnitOfTemperature.CELSIUS,
        entity_type=EntityType.DIAGNOSTIC,
        attribute_initialized_from_cache=False,
        translation_key="abs_max_heat_setpoint_limit",
        unique_id_suffix="abs_max_heat_setpoint_limit",
        fallback_name="Absolute maximum heating setpoint",
    )
    .sensor(
        attribute_name="sw_error_code",
        cluster_id=DIAGNOSTIC_CLUSTER_ID,
        attribute_converter=decode_system_status,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="system_status_code",
        unique_id_suffix="system_status_code",
        fallback_name="Danfoss system status",
    )
    .add_to_registry()
)
