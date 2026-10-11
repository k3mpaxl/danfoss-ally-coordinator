"""Config flow, setup, services and unload against a real Home Assistant core."""

from datetime import timedelta

import pytest
from homeassistant import config_entries
from homeassistant.components.switch import DATA_COMPONENT as SWITCH_COMPONENT
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.danfoss_ally_coordinator.const import DOMAIN
from custom_components.danfoss_ally_coordinator.diagnostics import (
    async_get_config_entry_diagnostics,
)

CELSIUS = {"unit_of_measurement": "°C"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load this repository's custom component."""
    return


@pytest.fixture
async def office(hass: HomeAssistant) -> dict[str, list]:
    """One ZHA Danfoss valve plus room sources from other integrations."""
    hass.config.components.add("zha")
    zha_entry = MockConfigEntry(domain="zha")
    zha_entry.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Office")
    devices = dr.async_get(hass)
    valve = devices.async_get_or_create(
        config_entry_id=zha_entry.entry_id,
        identifiers={("zha", "00:11")},
        manufacturer="Danfoss",
        model="eTRV0103",
        name="Valve",
    )
    devices.async_update_device(valve.id, area_id=area.id)
    entities = er.async_get(hass)

    def valve_entity(domain: str, suffix: str, object_id: str) -> str:
        return entities.async_get_or_create(
            domain,
            "zha",
            f"00:11-1{suffix}",
            suggested_object_id=object_id,
            device_id=valve.id,
            config_entry=zha_entry,
        ).entity_id

    def source(domain: str, object_id: str, device_class: str) -> str:
        entity_id = entities.async_get_or_create(
            domain,
            "demo",
            object_id,
            suggested_object_id=object_id,
            original_device_class=device_class,
        ).entity_id
        entities.async_update_entity(entity_id, area_id=area.id)
        return entity_id

    ids = {
        "climate": valve_entity("climate", "", "valve"),
        "external": valve_entity(
            "number", "-513-external_measured_room_sensor", "valve_external"
        ),
        "window": valve_entity(
            "switch", "-513-external_open_window_detected", "valve_window"
        ),
        "covered": valve_entity("switch", "-513-radiator_covered", "valve_covered"),
        "offset": valve_entity(
            "number", "-513-local_temperature_calibration", "valve_offset"
        ),
        "room": source("sensor", "office_temperature", "temperature"),
        "phantom": source("sensor", "office_phantom", "temperature"),
        "contact": source("binary_sensor", "office_window", "window"),
    }
    hass.states.async_set(
        ids["climate"], "heat", {"temperature": 20.0, "current_temperature": 20.5}
    )
    hass.states.async_set(ids["external"], "20.0", CELSIUS)
    hass.states.async_set(ids["window"], "off")
    hass.states.async_set(ids["covered"], "on")
    hass.states.async_set(ids["offset"], "0.0", {**CELSIUS, "min": -2.5, "max": 2.5})
    hass.states.async_set(ids["room"], "21.0", CELSIUS)
    hass.states.async_set(ids["phantom"], "30.0", CELSIUS)
    hass.states.async_set(ids["contact"], "on")

    calls: list[ServiceCall] = []

    async def number_set_value(call: ServiceCall) -> None:
        calls.append(call)
        entity_id = call.data["entity_id"]
        old = hass.states.get(entity_id)
        hass.states.async_set(entity_id, str(call.data["value"]), old.attributes)

    async def switch_turn(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        if entity := hass.data[SWITCH_COMPONENT].get_entity(entity_id):
            # This integration's own source switches are real entities.
            await getattr(entity, f"async_{call.service}")()
            return
        calls.append(call)
        state = "on" if call.service == "turn_on" else "off"
        hass.states.async_set(entity_id, state)

    # Load the real switch component first so its services do not replace the fakes.
    assert await async_setup_component(hass, "switch", {})
    hass.services.async_register("number", "set_value", number_set_value)
    hass.services.async_register("switch", "turn_on", switch_turn)
    hass.services.async_register("switch", "turn_off", switch_turn)

    async def climate_set_temperature(call: ServiceCall) -> None:
        calls.append(call)
        entity_id = call.data["entity_id"]
        old = hass.states.get(entity_id)
        attributes = {**old.attributes, "temperature": call.data["temperature"]}
        hass.states.async_set(entity_id, old.state, attributes)

    hass.services.async_register("climate", "set_temperature", climate_set_temperature)
    return {
        "ids": ids,
        "calls": calls,
        "area_id": area.id,
        "zha_entry": zha_entry,
        "valve_device_id": valve.id,
    }


def add_load_entities(hass, office, device_id: str, ieee: str, prefix: str) -> None:
    """Give a valve the ZHA entities used for catalogue §2.2 load balancing."""
    entities = er.async_get(hass)
    for domain, suffix, object_id, value in (
        ("sensor", "-513-load_estimate", f"{prefix}_load", "1000"),
        ("number", "-513-load_room_mean", f"{prefix}_load_mean", "-8000"),
        ("switch", "-513-load_balancing_enable", f"{prefix}_load_enable", "off"),
    ):
        entity_id = entities.async_get_or_create(
            domain,
            "zha",
            f"{ieee}-1{suffix}",
            suggested_object_id=object_id,
            device_id=device_id,
            config_entry=office["zha_entry"],
        ).entity_id
        hass.states.async_set(entity_id, value)


def add_second_valve(hass, office) -> dict[str, str]:
    """A second ZHA Danfoss valve in the same area, with load-balancing entities."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=office["zha_entry"].entry_id,
        identifiers={("zha", "00:22")},
        manufacturer="Danfoss",
        model="eTRV0103",
        name="Valve 2",
    )
    dr.async_get(hass).async_update_device(device.id, area_id=office["area_id"])
    entities = er.async_get(hass)
    ids = {}
    for key, domain, suffix in (
        ("climate", "climate", ""),
        ("external", "number", "-513-external_measured_room_sensor"),
        ("window", "switch", "-513-external_open_window_detected"),
    ):
        ids[key] = entities.async_get_or_create(
            domain,
            "zha",
            f"00:22-1{suffix}",
            suggested_object_id=f"valve2_{key}",
            device_id=device.id,
            config_entry=office["zha_entry"],
        ).entity_id
    hass.states.async_set(ids["climate"], "heat", {"temperature": 18.0})
    hass.states.async_set(ids["external"], "20.0", CELSIUS)
    hass.states.async_set(ids["window"], "off")
    add_load_entities(hass, office, device.id, "00:22", "valve2")
    add_load_entities(hass, office, office["valve_device_id"], "00:11", "valve1")
    return ids


async def test_config_flow_setup_services_options_and_unload(
    hass: HomeAssistant, office
) -> None:
    ids, calls = office["ids"], office["calls"]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"areas": [], "enabled": True, "max_sensor_age_minutes": 120}
    )
    assert result["errors"] == {"areas": "no_areas_selected"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "areas": [office["area_id"]],
            "enabled": True,
            "max_sensor_age_minutes": 120,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is config_entries.ConfigEntryState.LOADED

    # Mean of 21.0 and the phantom 30.0, written once; window opened.
    written = {(call.domain, call.service, call.data["entity_id"]) for call in calls}
    assert ("number", "set_value", ids["external"]) in written
    assert ("switch", "turn_on", ids["window"]) in written
    assert not any(call.data["entity_id"] == ids["covered"] for call in calls)
    assert hass.states.get(ids["external"]).state == "25.5"

    entity_registry = er.async_get(hass)
    own = [
        entity.entity_id
        for entity in entity_registry.entities.values()
        if entity.platform == DOMAIN
    ]
    mean_id = next(e for e in own if e.endswith("_room_temperature"))
    mean = hass.states.get(mean_id)
    assert mean.state == "25.5"
    assert mean.attributes["valves"][0]["mode"] == "covered"
    window_id = next(e for e in own if e.startswith("binary_sensor."))
    assert hass.states.get(window_id).state == "on"
    gap_id = next(e for e in own if "gap" in e)
    assert hass.states.get(gap_id).state == "unknown"

    # A closed contact is written promptly from the state-change event.
    hass.states.async_set(ids["contact"], "off")
    await hass.async_block_till_done()
    assert hass.states.get(ids["window"]).state == "off"

    # Calibration is refused in covered mode and changes nothing.
    with pytest.raises(HomeAssistantError, match="covered-radiator"):
        await hass.services.async_call(
            DOMAIN,
            "calibrate_local_sensor",
            {"entity_id": ids["climate"]},
            blocking=True,
            return_response=True,
        )
    assert hass.states.get(ids["offset"]).state == "0.0"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["rooms"][0]["valves"][0]["covered_entity_id"] == ids["covered"]

    # Excluding the phantom sensor through the options flow reloads the entry.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "areas": [office["area_id"]],
            "enabled": True,
            "max_sensor_age_minutes": 120,
            "sync_temperature": True,
            "sync_windows": True,
            "sync_valve_windows": False,
            "aggregation": "median",
            "excluded_entities": [ids["phantom"]],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get(mean_id).state == "21.0"
    sources = {
        source["entity_id"]: source["status"]
        for source in hass.states.get(mean_id).attributes["sources"]
    }
    assert sources == {ids["phantom"]: "ignored", ids["room"]: "used"}

    # A single-valve room gets no setpoint-sync or load-balancing switch.
    assert not [
        entity
        for entity in entity_registry.entities.values()
        if entity.platform == DOMAIN
        and entity.unique_id.endswith(("_setpoint_sync", "_load_balancing"))
    ]

    # Every source has a switch on the room device; it shows and controls usage.
    switches = {
        hass.states.get(entity_id).attributes["source_entity_id"]: entity_id
        for entity_id in hass.states.async_entity_ids("switch")
        if entity_id.startswith("switch.danfoss_ally_")
    }
    assert set(switches) == {ids["room"], ids["phantom"], ids["contact"]}
    phantom_switch = hass.states.get(switches[ids["phantom"]])
    assert phantom_switch.state == "off"
    assert phantom_switch.attributes["icon"] == "mdi:thermometer-off"
    assert phantom_switch.attributes["friendly_name"] == "Danfoss Ally – Office phantom"

    # Switching a source applies live, without reloading, and persists to options.
    coordinator = entry.runtime_data
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switches[ids["phantom"]]}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
    assert entry.options["excluded_entities"] == []
    assert hass.states.get(mean_id).state == "25.5"
    assert hass.states.get(switches[ids["phantom"]]).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switches[ids["contact"]]}, blocking=True
    )
    await hass.async_block_till_done()
    # Stored by registry ID, so renaming the entity ID does not lose the setting.
    contact_registry_id = er.async_get(hass).async_get(ids["contact"]).id
    assert entry.options["excluded_entities"] == [contact_registry_id]
    window = hass.states.get(window_id)
    assert window.state == "unknown"
    assert window.attributes["contacts"] == [
        {"entity_id": ids["contact"], "status": "ignored"}
    ]

    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.NOT_LOADED
    calls.clear()
    hass.states.async_set(ids["contact"], "on")
    await hass.async_block_till_done()
    assert calls == []


async def test_disabled_entry_never_writes(hass: HomeAssistant, office) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "areas": [office["area_id"]],
            "enabled": False,
            "max_sensor_age_minutes": 120,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(office["ids"]["contact"], "off")
    await hass.async_block_till_done()
    assert office["calls"] == []


async def test_setup_retries_when_no_valve_is_found(hass: HomeAssistant) -> None:
    hass.config.components.add("zha")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"areas": ["missing"], "enabled": False, "max_sensor_age_minutes": 120},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY


async def test_two_valve_room_syncs_setpoints_and_balances_load(
    hass: HomeAssistant, office
) -> None:
    valve2 = add_second_valve(hass, office)
    climate1 = office["ids"]["climate"]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "areas": [office["area_id"]],
            "enabled": True,
            "max_sensor_age_minutes": 120,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)

    def own(suffix: str) -> str:
        return next(
            entity.entity_id
            for entity in registry.entities.values()
            if entity.platform == DOMAIN and entity.unique_id.endswith(suffix)
        )

    sync_switch, load_switch = own("_setpoint_sync"), own("_load_balancing")
    assert hass.states.get(sync_switch).state == "on"
    assert hass.states.get(load_switch).state == "off"
    assert (
        hass.states.get(sync_switch)
        .attributes["friendly_name"]
        .endswith("Synchronize target temperature")
    )

    # Setpoint sync is on by default: valve 1 changing to 22 °C moves valve 2.
    coordinator = entry.runtime_data
    await coordinator.async_handle_setpoint_change(
        climate1,
        hass.states.get(climate1),
        State(climate1, "heat", {"temperature": 22.0}),
    )
    assert hass.states.get(valve2["climate"]).attributes["temperature"] == 22.0

    # Load balancing: on via its room switch, live and persisted.
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": load_switch}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
    assert entry.options["load_balancing_areas"] == [office["area_id"]]
    assert hass.states.get("switch.valve1_load_enable").state == "on"
    assert hass.states.get("number.valve2_load_mean").state == "1000"
    assert hass.states.get(load_switch).attributes["room_load_mean"] == 1000

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": load_switch}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("number.valve1_load_mean").state == "-8000"
    assert hass.states.get(load_switch).state == "off"


async def test_after_a_restart_valves_wait_for_the_zigbee_network(
    hass: HomeAssistant, office
) -> None:
    """No write during the start-up delay; temperatures need a fresh report."""
    ids, calls = office["ids"], office["calls"]
    hass.set_state(CoreState.not_running)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "areas": [office["area_id"]],
            "enabled": True,
            "max_sensor_age_minutes": 120,
            "startup_delay_minutes": 10,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    mean_id = next(
        entity.entity_id
        for entity in er.async_get(hass).entities.values()
        if entity.platform == DOMAIN and entity.unique_id.endswith("_mean_temperature")
    )
    assert hass.states.get(mean_id).attributes["writes_resume_at"] is not None
    # Values restored at start-up are not trusted until the sensor reports again.
    assert hass.states.get(mean_id).state == "unknown"

    # During the delay diagnostics follow the sources, but nothing is written.
    hass.states.async_set(ids["contact"], "off")
    await hass.async_block_till_done()
    hass.states.async_set(ids["contact"], "on")
    hass.states.async_set(ids["room"], "21.5", CELSIUS)
    await hass.async_block_till_done()
    assert hass.states.get(mean_id).state == "21.5"
    assert calls == []

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert hass.states.get(ids["window"]).state == "on"
    assert hass.states.get(ids["external"]).state == "21.5"
    assert hass.states.get(mean_id).attributes["writes_resume_at"] is None


async def test_incomplete_valve_raises_a_repair_issue(
    hass: HomeAssistant, office
) -> None:
    devices = dr.async_get(hass)
    bare = devices.async_get_or_create(
        config_entry_id=office["zha_entry"].entry_id,
        identifiers={("zha", "00:33")},
        manufacturer="Danfoss",
        model="eTRV0103",
        name="Bare valve",
    )
    devices.async_update_device(bare.id, area_id=office["area_id"])
    er.async_get(hass).async_get_or_create(
        "climate", "zha", "00:33-1", device_id=bare.id, config_entry=office["zha_entry"]
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "areas": [office["area_id"]],
            "enabled": False,
            "max_sensor_age_minutes": 120,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"incomplete_valve_{bare.id}")
    assert issue is not None
    assert issue.translation_placeholders["valve"] == "Bare valve"
    assert "external_measured_room_sensor" in issue.translation_placeholders["missing"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"incomplete_valve_{bare.id}")
        is None
    )


async def test_renamed_source_keeps_its_setting_and_switch(
    hass: HomeAssistant, office
) -> None:
    """Entity-ID migrations must not lose ignored sources or source switches."""
    ids = office["ids"]
    registry = er.async_get(hass)
    phantom_id = registry.async_get(ids["phantom"]).id
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "areas": [office["area_id"]],
            "enabled": False,
            "max_sensor_age_minutes": 120,
        },
        # Legacy form: an entity ID instead of a registry ID.
        options={"excluded_entities": [ids["phantom"]]},
    )
    entry.add_to_hass(hass)
    # A pre-release switch keyed by entity ID is migrated, not duplicated.
    legacy = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{office['area_id']}_source_{ids['phantom']}",
        config_entry=entry,
        suggested_object_id="legacy_phantom_switch",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(legacy.entity_id).unique_id == (
        f"{office['area_id']}_source_{phantom_id}"
    )

    registry.async_update_entity(ids["phantom"], new_entity_id="sensor.office_shelf")
    hass.states.async_set("sensor.office_shelf", "30.0", CELSIUS)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert "sensor.office_shelf" in coordinator.excluded_entity_ids
    assert "sensor.office_shelf" in coordinator.rooms[0].temperature_entity_ids
    switches = [
        e
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.unique_id.endswith(f"_source_{phantom_id}")
    ]
    assert len(switches) == 1
    assert hass.states.get(switches[0].entity_id).state == "off"


async def test_rooms_no_longer_coordinated_are_removed(
    hass: HomeAssistant, office
) -> None:
    """A room whose area was replaced leaves no device or entity behind."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "areas": [office["area_id"]],
            "enabled": False,
            "max_sensor_age_minutes": 120,
        },
    )
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    old_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "old_area")},
        name="Danfoss Ally – Office",
    )
    old_entity = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "old_area_mean_temperature",
        config_entry=entry,
        device_id=old_device.id,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(old_entity.entity_id) is None
    assert devices.async_get(old_device.id) is None
    remaining = dr.async_entries_for_config_entry(devices, entry.entry_id)
    assert [device.identifiers for device in remaining] == [
        {(DOMAIN, office["area_id"])}
    ]
    assert er.async_entries_for_device(er.async_get(hass), remaining[0].id)


async def test_heat_source_is_forwarded_to_the_valves(
    hass: HomeAssistant, office
) -> None:
    """The configured heat source reaches the valve's HeatAvailable switch."""
    entities = er.async_get(hass)
    heat_switch = entities.async_get_or_create(
        "switch",
        "zha",
        "00:11-1-513-heat_available",
        suggested_object_id="valve_heat_available",
        device_id=office["valve_device_id"],
        config_entry=office["zha_entry"],
    ).entity_id
    hass.states.async_set(heat_switch, "off")
    source = entities.async_get_or_create(
        "binary_sensor", "ecl310", "heat_available", suggested_object_id="ecl_heat"
    )
    hass.states.async_set(source.entity_id, "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "areas": [office["area_id"]],
            "enabled": True,
            "max_sensor_age_minutes": 120,
            "startup_delay_minutes": 0,
            "heat_source": source.id,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(heat_switch).state == "on"
    office["calls"].clear()
    hass.states.async_set(source.entity_id, "off")
    await hass.async_block_till_done()
    assert hass.states.get(heat_switch).state == "off"
    assert [call.data["entity_id"] for call in office["calls"]] == [heat_switch]
