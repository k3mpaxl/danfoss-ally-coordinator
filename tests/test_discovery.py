"""Discovery behavior for room-scoped Danfoss coordination."""

import pytest

from custom_components.danfoss_ally_coordinator.discovery import (
    DANFOSS_MODELS,
    DeviceRecord,
    EntityRecord,
    IncompleteValve,
    discover_rooms,
)

# The Ally radiator thermostat reports its hardware revision as the Zigbee model.
ALLY_MODELS = ("eTRV0100", "eTRV0101", "eTRV0103")


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


def _zha(entity_id, domain, unique_id, device_id, *, disabled=False):
    return EntityRecord(
        entity_id, domain, "zha", unique_id, device_id, None, None, disabled
    )


def _complete_valve(device_id: str) -> tuple[EntityRecord, ...]:
    """A valve's climate entity plus the two control entities ZHA's quirk adds."""
    return (
        _zha(f"climate.{device_id}", "climate", f"{device_id}-1", device_id),
        _zha(
            f"number.{device_id}_external",
            "number",
            f"{device_id}-1-513-external_measured_room_sensor",
            device_id,
        ),
        _zha(
            f"switch.{device_id}_window",
            "switch",
            f"{device_id}-1-513-external_open_window_detected",
            device_id,
        ),
    )


def test_supported_models_are_the_ally_revisions() -> None:
    """The set is deliberate: not every model that ZHA's Danfoss quirk matches."""
    assert DANFOSS_MODELS == set(ALLY_MODELS)


@pytest.mark.parametrize("model", ALLY_MODELS)
def test_every_ally_revision_with_a_climate_entity_is_discovered(model: str) -> None:
    rooms = discover_rooms(
        devices=(DeviceRecord("valve", "Danfoss", model, "office", "Valve"),),
        entities=_complete_valve("valve"),
        areas={"office": "Office"},
        selected_area_ids={"office"},
    )

    assert len(rooms) == 1
    assert rooms[0].incomplete_valves == ()
    valve = rooms[0].valves[0]
    assert (valve.device_id, valve.name) == ("valve", "Valve")
    assert valve.climate_entity_id == "climate.valve"
    assert valve.temperature_entity_id == "number.valve_external"
    assert valve.window_entity_id == "switch.valve_window"


@pytest.mark.parametrize("model", ALLY_MODELS)
def test_ally_revision_without_control_entities_is_incomplete_not_controlled(
    model: str,
) -> None:
    """The safety net does not depend on the model: no control entities, no writes."""
    devices = (
        DeviceRecord("bare", "Danfoss", model, "office", "Bare"),
        DeviceRecord("half", "Danfoss", model, "office", "Half"),
    )
    entities = (
        _zha("climate.bare", "climate", "bare-1", "bare"),
        _zha("climate.half", "climate", "half-1", "half"),
        _zha(
            "number.half_external",
            "number",
            "half-1-513-external_measured_room_sensor",
            "half",
        ),
        _zha(
            "switch.half_window",
            "switch",
            "half-1-513-external_open_window_detected",
            "half",
            disabled=True,
        ),
    )

    rooms = discover_rooms(
        devices=devices,
        entities=entities,
        areas={"office": "Office"},
        selected_area_ids={"office"},
    )

    assert len(rooms) == 1
    assert rooms[0].valves == ()
    assert rooms[0].incomplete_valves == (
        IncompleteValve(
            "bare",
            "Bare",
            ("external_measured_room_sensor", "external_open_window_detected"),
        ),
        IncompleteValve("half", "Half", ("external_open_window_detected (disabled)",)),
    )


def test_both_revisions_in_one_room_are_coordinated_together() -> None:
    """Valves bought years apart report different models but share the room."""
    rooms = discover_rooms(
        devices=(
            DeviceRecord("old", "Danfoss", "eTRV0100", "office", "Old"),
            DeviceRecord("new", "Danfoss", "eTRV0103", "office", "New"),
        ),
        entities=(*_complete_valve("old"), *_complete_valve("new")),
        areas={"office": "Office"},
        selected_area_ids={"office"},
    )

    assert [valve.device_id for valve in rooms[0].valves] == ["new", "old"]


@pytest.mark.parametrize(
    ("manufacturer", "model"),
    [
        ("Danfoss", "eTRV0102"),  # not a model ZHA's quirk matches
        ("Danfoss", "TRV001"),  # Hive-branded: matched by the quirk, never run here
        ("D5X84YU", "eT093WRO"),  # Popp-branded: another manufacturer string
        ("Acme", "eTRV0100"),  # the manufacturer check stays
        (None, "eTRV0100"),
    ],
)
def test_other_devices_are_not_controlled_even_with_all_entities(
    manufacturer: str | None, model: str
) -> None:
    rooms = discover_rooms(
        devices=(DeviceRecord("valve", manufacturer, model, "office", "Valve"),),
        entities=_complete_valve("valve"),
        areas={"office": "Office"},
        selected_area_ids={"office"},
    )

    assert rooms == ()
