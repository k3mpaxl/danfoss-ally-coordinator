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
    assert room.valves[0].priority_entity_id == "switch.office_covered"


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
