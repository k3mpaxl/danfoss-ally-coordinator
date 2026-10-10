import asyncio
from unittest.mock import MagicMock

from zha.quirks import DEVICE_REGISTRY
from zhaquirks.danfoss.thermostat import (
    DanfossDiagnosticCluster,
    DanfossThermostatCluster,
    DanfossTimeCluster,
    DanfossUserInterfaceCluster,
)
from zigpy.device import Device
from zigpy.types import EUI64, NWK
from zigpy.zcl import Cluster
from zigpy.zcl.clusters.general import Time
from zigpy.zcl.clusters.homeautomation import Diagnostic
from zigpy.zcl.clusters.hvac import Thermostat, UserInterface

from zha_custom_quirks.danfoss_ally_optimized import (
    DANFOSS_MANUFACTURER_CODE,
    QUIRK_ENTRY,
    SCHEDULED_SETPOINT_ALIAS_ID,
    DanfossAllyOptimizedThermostatCluster,
    DanfossProgrammingOperationMode,
    decode_system_status,
    describe_external_room_sensor,
    describe_room_load,
    format_exercise_time,
    zcl_setpoint,
)


def test_danfoss_error_bitmap_decodes_to_z2m_style_status():
    assert decode_system_status(0) == "ok"
    assert (
        decode_system_status(0x2000 | 0x0200) == "invalid_clock_information,low_battery"
    )


def test_programming_mode_uses_documented_z2m_values():
    assert {mode.value: mode.name for mode in DanfossProgrammingOperationMode} == {
        0: "setpoint",
        1: "schedule",
        3: "schedule_with_preheat",
        4: "eco",
    }


def test_human_readable_valve_exercise_time():
    assert format_exercise_time(660) == "11:00"
    assert format_exercise_time(0) == "00:00"
    assert format_exercise_time(1439) == "23:59"
    assert format_exercise_time(1440) == "Nicht verfügbar"


def test_room_load_sentinel_is_explained_not_presented_as_a_value():
    assert describe_room_load(-8000) == "Nicht verfügbar (Lastverteilung inaktiv)"
    assert describe_room_load(-700) == "Ungültig (-700)"
    assert describe_room_load(1200) == "Aktiv (Steuerwert: 1200)"


def test_external_sensor_sentinel_is_explained_not_shown_as_minus_80_degrees():
    assert describe_external_room_sensor(-8000) == "Nicht verbunden / deaktiviert"
    assert describe_external_room_sensor(2155) == "21.6 °C"


def test_setpoint_encoding_uses_zcl_hundredths_of_a_degree():
    assert zcl_setpoint(20.5) == 2050
    assert zcl_setpoint(5) == 500
    assert zcl_setpoint(35) == 3500


def test_setpoint_encoding_rejects_values_outside_danfoss_range():
    for value in (4.5, 35.5, 20.25):
        try:
            zcl_setpoint(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected {value} °C to be rejected")


def test_custom_v2_entry_targets_etrv0103_and_adds_gap_and_usability_entities():
    assert QUIRK_ENTRY.device_match.applies_to[0].manufacturer == "Danfoss"
    assert QUIRK_ENTRY.device_match.applies_to[0].model == "eTRV0103"
    metadata = QUIRK_ENTRY.zha_device_factory.quirk_definition.entity_metadata
    names = {item.translation_key for item in metadata}
    assert names == {
        "programming_operation_mode",
        "occupied_heating_setpoint_scheduled",
        "abs_max_heat_setpoint_limit",
        "system_status_code",
        "exercise_time",
        "room_load_status",
        "external_room_sensor_status",
    }


def test_scheduled_alias_reads_back_real_occupied_setpoint():
    cluster = DanfossAllyOptimizedThermostatCluster(MagicMock())
    real_attr = cluster.AttributeDefs.occupied_heating_setpoint.id
    cluster._update_attribute(real_attr, 2050)

    success, failure = asyncio.run(
        cluster.read_attributes(["scheduled_setpoint_input"], allow_cache=True)
    )

    assert success == {"scheduled_setpoint_input": 2050}
    assert failure == {}
    assert cluster.get("scheduled_setpoint_input") == 2050


def test_scheduled_write_is_slow_manufacturer_specific_attribute_not_fast_command(
    monkeypatch,
):
    captured = {}

    async def fake_cluster_write(self, attributes, manufacturer=None, **kwargs):
        captured.update(attributes=attributes, manufacturer=manufacturer)
        return [[]]

    monkeypatch.setattr(Cluster, "write_attributes", fake_cluster_write)
    cluster = DanfossAllyOptimizedThermostatCluster(MagicMock())

    result = asyncio.run(cluster.write_attributes({"scheduled_setpoint_input": 2050}))

    assert result == [[]]
    assert captured == {
        "attributes": {"occupied_heating_setpoint": 2050},
        "manufacturer": DANFOSS_MANUFACTURER_CODE,
    }
    assert cluster.get(SCHEDULED_SETPOINT_ALIAS_ID) == 2050


def test_fast_setpoint_write_remains_owned_by_upstream_quirk(monkeypatch):
    cluster = DanfossAllyOptimizedThermostatCluster(MagicMock())
    captured = {}

    async def fake_fast_write(self, attributes, manufacturer=None, **kwargs):
        captured["attributes"] = attributes
        captured["manufacturer"] = manufacturer
        return [[]]

    monkeypatch.setattr(DanfossThermostatCluster, "write_attributes", fake_fast_write)
    result = asyncio.run(cluster.write_attributes({"occupied_heating_setpoint": 2050}))

    assert result == [[]]
    assert captured["attributes"] == {"occupied_heating_setpoint": 2050}


def test_registry_applies_optimized_overlay_and_preserves_upstream_clusters():
    device = Device(MagicMock(), EUI64.convert("00:11:22:33:44:55:66:77"), NWK(0x1234))
    device.manufacturer = "Danfoss"
    device.model = "eTRV0103"
    endpoint = device.add_endpoint(1)
    for cluster in (Thermostat, UserInterface, Diagnostic, Time):
        endpoint.add_input_cluster(cluster.cluster_id)

    assert DEVICE_REGISTRY.match_entry(device) is QUIRK_ENTRY
    resolved = DEVICE_REGISTRY.resolve(device)
    clusters = resolved.endpoints[1].in_clusters
    assert isinstance(
        clusters[Thermostat.cluster_id], DanfossAllyOptimizedThermostatCluster
    )
    assert isinstance(clusters[UserInterface.cluster_id], DanfossUserInterfaceCluster)
    assert isinstance(clusters[Diagnostic.cluster_id], DanfossDiagnosticCluster)
    assert isinstance(clusters[Time.cluster_id], DanfossTimeCluster)


def test_user_facing_room_load_and_exercise_time_metadata_is_present():
    definition = QUIRK_ENTRY.zha_device_factory.quirk_definition
    metadata = {item.unique_id_suffix: item for item in definition.entity_metadata}
    assert metadata["exercise_time"].fallback_name == "Ventilübungszeit"
    assert metadata["room_load_status"].fallback_name == "Status der Raumlastverteilung"
    assert (
        metadata["external_room_sensor_status"].fallback_name
        == "Status externes Raumthermometer"
    )

    changed = {
        item.unique_id_suffix: item for item in definition.changed_entity_metadata
    }
    assert (
        changed["exercise_trigger_time"].new_fallback_name
        == "Ventilübungszeit (Minuten nach Mitternacht)"
    )
    assert (
        changed["load_room_mean"].new_fallback_name
        == "Raumlast-Mittelwert (Gateway-Steuerwert)"
    )
    external_sensor = changed["external_measured_room_sensor"]
    assert (
        external_sensor.new_fallback_name
        == "Externes Raumtemperatur-Signal (−80 °C = deaktiviert)"
    )
    assert external_sensor.new_entity_registry_enabled_default is not False
