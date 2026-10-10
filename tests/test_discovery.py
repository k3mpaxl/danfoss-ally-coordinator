"""Discovery behavior for room-scoped Danfoss coordination."""

from custom_components.danfoss_ally_coordinator.discovery import (
    DeviceRecord,
    EntityRecord,
    discover_rooms,
)


def test_discovery_groups_zha_danfoss_valves_and_all_area_temperature_sources() -> None:
    """Only ZHA eTRVs become targets; cross-integration sources remain inputs."""
    devices = (
        DeviceRecord("trv-1", "Danfoss", "eTRV0103", "office"),
        DeviceRecord("sensor-1", "Acme", "Air", "office"),
        DeviceRecord("other-trv", "Danfoss", "eTRV0103", "other"),
    )
    entities = (
        EntityRecord(
            "climate.office_valve", "climate", "zha", "climate", "trv-1", None, None
        ),
        EntityRecord(
            "number.office_external_temperature",
            "number",
            "zha",
            "00:11-1-513-external_measured_room_sensor",
            "trv-1",
            None,
            None,
        ),
        EntityRecord(
            "switch.office_external_window",
            "switch",
            "zha",
            "00:11-1-513-external_open_window_detected",
            "trv-1",
            None,
            None,
        ),
        EntityRecord(
            "switch.office_covered",
            "switch",
            "zha",
            "00:11-1-513-radiator_covered",
            "trv-1",
            None,
            None,
        ),
        EntityRecord(
            "binary_sensor.office_trv_mounting",
            "binary_sensor",
            "zha",
            "00:11-1-513-mounting_mode_active",
            "trv-1",
            None,
            "opening",
        ),
        EntityRecord(
            "sensor.office_air",
            "sensor",
            "matter",
            "air-temperature",
            "sensor-1",
            None,
            "temperature",
        ),
        EntityRecord(
            "binary_sensor.office_window",
            "binary_sensor",
            "matter",
            "window-state",
            "sensor-1",
            None,
            "opening",
        ),
        EntityRecord(
            "binary_sensor.office_door",
            "binary_sensor",
            "homekit",
            "door-state",
            None,
            "office",
            "door",
        ),
        EntityRecord(
            "climate.other_valve", "climate", "zha", "climate", "other-trv", None, None
        ),
    )

    rooms = discover_rooms(
        devices=devices,
        entities=entities,
        areas={"office": "Büro", "other": "Anderer Raum"},
        selected_area_ids={"office"},
    )

    assert len(rooms) == 1
    room = rooms[0]
    assert room.area_id == "office"
    assert room.name == "Büro"
    assert room.temperature_entity_ids == ("sensor.office_air",)
    assert room.window_entity_ids == (
        "binary_sensor.office_door",
        "binary_sensor.office_window",
    )
    assert room.valves[0].temperature_entity_id == "number.office_external_temperature"
    assert room.valves[0].window_entity_id == "switch.office_external_window"
    # Covered mode is discovered read-only to select the write cadence.
    assert room.valves[0].covered_entity_id == "switch.office_covered"


def test_discovery_lists_all_candidates_and_names_valves() -> None:
    """Every area candidate is listed; the user switches unwanted ones off later."""
    devices = (
        DeviceRecord("trv-1", "Danfoss", "eTRV0103", "office", "Heizung Fenster"),
        DeviceRecord("motion", "Signify", "SML003", "office"),
    )
    entities = (
        EntityRecord("climate.valve", "climate", "zha", "ieee-1", "trv-1", None, None),
        EntityRecord(
            "number.external",
            "number",
            "zha",
            "ieee-1-513-external_measured_room_sensor",
            "trv-1",
            None,
            None,
        ),
        EntityRecord(
            "switch.window",
            "switch",
            "zha",
            "ieee-1-513-external_open_window_detected",
            "trv-1",
            None,
            None,
        ),
        EntityRecord(
            "binary_sensor.motion_opening",
            "binary_sensor",
            "zha",
            "ieee-2-6",
            "motion",
            None,
            "opening",
        ),
        EntityRecord(
            "sensor.motion_temperature",
            "sensor",
            "zha",
            "ieee-2-1026",
            "motion",
            None,
            "temperature",
        ),
    )

    rooms = discover_rooms(
        devices=devices,
        entities=entities,
        areas={"office": "Büro"},
        selected_area_ids={"office"},
    )

    assert rooms[0].window_entity_ids == ("binary_sensor.motion_opening",)
    assert rooms[0].temperature_entity_ids == ("sensor.motion_temperature",)
    assert rooms[0].valves[0].name == "Heizung Fenster"


def test_discovery_excludes_zha_device_temperature_entities() -> None:
    """A ZHA device-temperature diagnostic is not a room sensor source."""
    devices = (DeviceRecord("trv-1", "Danfoss", "eTRV0103", "office"),)
    entities = (
        EntityRecord("climate.valve", "climate", "zha", "climate", "trv-1", None, None),
        EntityRecord(
            "number.external",
            "number",
            "zha",
            "00:11-1-513-external_measured_room_sensor",
            "trv-1",
            None,
            None,
        ),
        EntityRecord(
            "switch.window",
            "switch",
            "zha",
            "00:11-1-513-external_open_window_detected",
            "trv-1",
            None,
            None,
        ),
        EntityRecord(
            "sensor.device_temperature",
            "sensor",
            "zha",
            "device_temperature",
            "sensor-device",
            "office",
            "temperature",
            translation_key="device_temperature",
        ),
        EntityRecord(
            "sensor.room_temperature",
            "sensor",
            "zha",
            "room_temperature",
            "room-sensor",
            "office",
            "temperature",
        ),
    )

    rooms = discover_rooms(
        devices=devices,
        entities=entities,
        areas={"office": "Büro"},
        selected_area_ids={"office"},
    )

    assert rooms[0].temperature_entity_ids == ("sensor.room_temperature",)


def test_discovery_finds_optional_valve_controls_by_registry_identity() -> None:
    device = DeviceRecord("valve", "Danfoss", "eTRV0103", "office")
    entities = (
        EntityRecord("climate.valve", "climate", "zha", "ieee-1", "valve", None, None),
        EntityRecord(
            "number.external",
            "number",
            "zha",
            "ieee-external_measured_room_sensor",
            "valve",
            None,
            None,
        ),
        EntityRecord(
            "switch.window",
            "switch",
            "zha",
            "ieee-external_open_window_detected",
            "valve",
            None,
            None,
        ),
        EntityRecord(
            "sensor.window",
            "sensor",
            "zha",
            "ieee-open_window_detection",
            "valve",
            None,
            None,
        ),
        EntityRecord(
            "sensor.source",
            "sensor",
            "zha",
            "ieee-setpoint_change_source",
            "valve",
            None,
            None,
        ),
        EntityRecord(
            "number.offset",
            "number",
            "zha",
            "ieee-local_temperature_calibration",
            "valve",
            None,
            None,
        ),
    )
    valve = discover_rooms(
        devices=(device,),
        entities=entities,
        areas={"office": "Office"},
        selected_area_ids={"office"},
    )[0].valves[0]
    assert valve.climate_entity_id == "climate.valve"
    assert valve.window_detection_entity_id == "sensor.window"
    assert valve.setpoint_source_entity_id == "sensor.source"
    assert valve.calibration_entity_id == "number.offset"


def test_valve_without_control_entities_is_reported_not_dropped() -> None:
    """Missing or disabled ZHA entities make a valve visible as incomplete."""
    devices = (
        DeviceRecord("full", "Danfoss", "eTRV0103", "office", "Full"),
        DeviceRecord("bare", "Danfoss", "eTRV0103", "office", "Bare"),
        DeviceRecord("lonely", "Danfoss", "eTRV0103", "bedroom", "Lonely"),
    )

    def zha(entity_id, domain, unique_id, device_id, *, disabled=False):
        return EntityRecord(
            entity_id, domain, "zha", unique_id, device_id, None, None, disabled
        )

    entities = (
        zha("climate.full", "climate", "f-1", "full"),
        zha("number.full", "number", "f-1-513-external_measured_room_sensor", "full"),
        zha("switch.full", "switch", "f-1-513-external_open_window_detected", "full"),
        zha("sensor.full_errors", "sensor", "f-1-2821-sw_error_code", "full"),
        zha("climate.bare", "climate", "b-1", "bare"),
        zha(
            "switch.bare",
            "switch",
            "b-1-513-external_open_window_detected",
            "bare",
            disabled=True,
        ),
        zha("climate.lonely", "climate", "l-1", "lonely"),
    )

    rooms = discover_rooms(
        devices=devices,
        entities=entities,
        areas={"office": "Office", "bedroom": "Bedroom"},
        selected_area_ids={"office", "bedroom"},
    )

    by_area = {room.area_id: room for room in rooms}
    office, bedroom = by_area["office"], by_area["bedroom"]
    assert [valve.device_id for valve in office.valves] == ["full"]
    assert office.valves[0].error_entity_id == "sensor.full_errors"
    assert office.incomplete_valves[0].device_id == "bare"
    assert office.incomplete_valves[0].missing == (
        "external_measured_room_sensor",
        "external_open_window_detected (disabled)",
    )
    # An area whose only valve is incomplete still shows up.
    assert bedroom.valves == ()
    assert bedroom.incomplete_valves[0].name == "Lonely"
