"""End-to-end coordinator behavior using only typed existing ZHA entities."""

import asyncio
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import Context, State
from homeassistant.exceptions import HomeAssistantError

from custom_components.danfoss_ally_coordinator.coordinator import RoomCoordinator
from custom_components.danfoss_ally_coordinator.discovery import (
    RoomDefinition,
    ValveTarget,
)
from custom_components.danfoss_ally_coordinator.sensor import LocalTemperatureGapSensor
from custom_components.danfoss_ally_coordinator.store import SyncStateStore

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
CELSIUS = {"unit_of_measurement": "°C"}


def state(
    entity_id: str,
    value: str,
    attributes: dict[str, object] | None = None,
    *,
    updated: datetime = NOW,
    reported: datetime | None = None,
    context: Context | None = None,
) -> State:
    """Create a state whose change and report times are fully controlled."""
    return State(
        entity_id,
        value,
        attributes or {},
        last_changed=updated,
        last_updated=updated,
        last_reported=reported or updated,
        context=context,
    )


class FakeStates:
    """Small state registry compatible with the coordinator's read-only use."""

    def __init__(self, values: dict[str, State]) -> None:
        self._values = values

    def get(self, entity_id: str) -> State | None:
        return self._values.get(entity_id)

    def set(self, new_state: State) -> None:
        self._values[new_state.entity_id] = new_state


class FakeServices:
    """Records typed HA service calls without contacting a live device."""

    def __init__(self, states: FakeStates) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict[str, object], bool]] = []

    async def async_call(
        self, domain: str, service: str, data: dict[str, object], *, blocking: bool
    ) -> None:
        self.calls.append((domain, service, data, blocking))
        if "entity_id" not in data:  # e.g. zha.set_zigbee_cluster_attribute (by IEEE)
            return
        entity_id = data["entity_id"]
        old = self.states.get(entity_id)
        attributes = dict(old.attributes) if old is not None else {}
        if domain == "number" and service == "set_value":
            self.states.set(State(entity_id, str(data["value"]), attributes or CELSIUS))
        elif domain == "switch":
            self.states.set(
                State(entity_id, "on" if service == "turn_on" else "off", attributes)
            )
        elif domain == "select":
            self.states.set(State(entity_id, str(data["option"]), attributes))
        elif domain == "climate" and service == "set_temperature":
            attributes["temperature"] = data["temperature"]
            self.states.set(State(entity_id, old.state, attributes))


class FakeHass:
    """The minimal HA surface used for one evaluation."""

    def __init__(self, values: dict[str, State]) -> None:
        self.states = FakeStates(values)
        self.services = FakeServices(self.states)
        self.config = SimpleNamespace(time_zone="Europe/Berlin")

    def async_create_task(self, coroutine):
        return asyncio.get_running_loop().create_task(coroutine)


def written(hass: FakeHass) -> list[str]:
    return [call[2]["entity_id"] for call in hass.services.calls]


def switch_and_settle(coordinator: RoomCoordinator, change) -> None:
    """Flip a room switch, then wait for its background radio work."""

    async def run() -> None:
        await change
        await asyncio.gather(*coordinator._tasks)

    asyncio.run(run())


def evaluate(coordinator: RoomCoordinator, **kwargs) -> None:
    asyncio.run(coordinator.async_evaluate_all(now=NOW, reason="test", **kwargs))


def test_enabled_coordinator_averages_room_temperature_and_syncs_window_state() -> None:
    """One selected room writes its mean and any-open window state to each eTRV."""
    room = RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(
            ValveTarget(
                "trv-1",
                "number.trv_external_temperature",
                "switch.trv_external_window",
                "switch.trv_covered",
            ),
        ),
        temperature_entity_ids=("sensor.a", "sensor.b"),
        window_entity_ids=("binary_sensor.window",),
    )
    hass = FakeHass(
        {
            "sensor.a": state("sensor.a", "20.0", CELSIUS),
            "sensor.b": state("sensor.b", "22.0", CELSIUS),
            "binary_sensor.window": state("binary_sensor.window", "on"),
            "switch.trv_covered": state("switch.trv_covered", "off"),
            "switch.trv_external_window": state("switch.trv_external_window", "off"),
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=True, max_sensor_age=timedelta(hours=2)
    )

    evaluate(coordinator)

    assert coordinator.statuses["office"].mean_temperature == 21.0
    assert coordinator.statuses["office"].window_open
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.trv_external_temperature", "value": 21.0},
            True,
        ),
        ("switch", "turn_on", {"entity_id": "switch.trv_external_window"}, True),
    ]


def test_feature_toggles_can_disable_temperature_and_window_writes() -> None:
    """The master opt-in can keep each individual radio feature disabled."""
    room = RoomDefinition(
        "office",
        "Büro",
        (ValveTarget("trv-1", "number.trv", "switch.window"),),
        ("sensor.room",),
        ("binary_sensor.window",),
    )
    hass = FakeHass(
        {
            "sensor.room": state("sensor.room", "21.0", CELSIUS),
            "binary_sensor.window": state("binary_sensor.window", "on"),
            "number.trv": state("number.trv", "20.0", CELSIUS),
            "switch.window": state("switch.window", "off"),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        sync_temperature=False,
        sync_windows=False,
        max_sensor_age=timedelta(hours=2),
    )

    evaluate(coordinator)

    assert hass.services.calls == []


def test_setup_dry_run_calculates_without_writing() -> None:
    """Before Home Assistant has started, diagnostics exist but no valve is written."""
    room = RoomDefinition(
        "office",
        "Büro",
        (ValveTarget("trv-1", "number.trv", "switch.window"),),
        ("sensor.room",),
        ("binary_sensor.window",),
    )
    hass = FakeHass(
        {
            "sensor.room": state("sensor.room", "21.0", CELSIUS),
            "binary_sensor.window": state("binary_sensor.window", "on"),
            "switch.window": state("switch.window", "off"),
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=True, max_sensor_age=timedelta(hours=2)
    )

    evaluate(coordinator, allow_writes=False)

    assert coordinator.statuses["office"].mean_temperature == 21.0
    assert coordinator.statuses["office"].window_open is True
    assert hass.services.calls == []


def test_source_count_matches_only_readings_used_by_the_mean() -> None:
    """Diagnostics do not claim invalid/no-unit values contributed to a mean."""
    room = RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(ValveTarget("trv-1", "number.trv", "switch.trv_window"),),
        temperature_entity_ids=(
            "sensor.good",
            "sensor.no_unit",
            "sensor.nan",
            "sensor.old",
        ),
        window_entity_ids=(),
    )
    coordinator = RoomCoordinator(
        FakeHass(
            {
                "sensor.good": state("sensor.good", "20.0", CELSIUS),
                "sensor.no_unit": state("sensor.no_unit", "21.0"),
                "sensor.nan": state("sensor.nan", "nan", CELSIUS),
                "sensor.old": state(
                    "sensor.old", "22.0", CELSIUS, updated=NOW - timedelta(hours=3)
                ),
                "switch.trv_window": state("switch.trv_window", "off"),
            }
        ),
        rooms=(room,),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )

    evaluate(coordinator)

    assert coordinator.statuses["office"].mean_temperature == 20.0
    assert coordinator.statuses["office"].valid_temperature_sources == 1


def test_steady_sensor_stays_valid_while_it_keeps_reporting() -> None:
    """An unchanged value confirmed by a recent report is fresh, not stale."""
    room = RoomDefinition(
        "office",
        "Büro",
        (ValveTarget("trv-1", "number.trv", "switch.window"),),
        ("sensor.steady",),
        (),
    )
    coordinator = RoomCoordinator(
        FakeHass(
            {
                "sensor.steady": state(
                    "sensor.steady",
                    "21.5",
                    CELSIUS,
                    updated=NOW - timedelta(hours=5),
                    reported=NOW - timedelta(minutes=10),
                )
            }
        ),
        rooms=(room,),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )

    evaluate(coordinator)

    assert coordinator.statuses["office"].mean_temperature == 21.5
    assert coordinator.statuses["office"].valid_temperature_sources == 1


def test_successful_temperature_sync_is_persisted_for_the_next_restart() -> None:
    """A restart retains the last value/time and does not reset the radio cadence."""
    room = RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(
            ValveTarget("trv-1", "number.trv_external_temperature", "switch.window"),
        ),
        temperature_entity_ids=("sensor.room",),
        window_entity_ids=(),
    )
    hass = FakeHass(
        {
            "sensor.room": state("sensor.room", "21.0", CELSIUS),
            "switch.window": state("switch.window", "off"),
        }
    )

    class MemoryStore:
        saved: dict[str, object] | None = None

        async def async_load(self) -> None:
            return None

        async def async_save(self, data: dict[str, object]) -> None:
            self.saved = data

    memory = MemoryStore()
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        sync_state_store=SyncStateStore(memory),
    )

    evaluate(coordinator)

    assert memory.saved == {
        "records": {
            "number.trv_external_temperature": {
                "value": 21.0,
                "succeeded_at": "2026-10-09T12:00:00+00:00",
            }
        }
    }


def test_refresh_replaces_discovered_rooms_without_restarting_the_entry() -> None:
    """A refresh can safely adopt updated area membership and source lists."""
    initial_room = RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(ValveTarget("trv-1", "number.a", "switch.a"),),
        temperature_entity_ids=("sensor.office",),
        window_entity_ids=(),
    )
    refreshed_room = RoomDefinition(
        area_id="bedroom",
        name="Schlafzimmer",
        valves=(ValveTarget("trv-2", "number.b", "switch.b"),),
        temperature_entity_ids=("sensor.bedroom",),
        window_entity_ids=("binary_sensor.bedroom_window",),
    )
    coordinator = RoomCoordinator(
        FakeHass({}),
        rooms=(initial_room,),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )

    async def discover():
        return (refreshed_room,)

    asyncio.run(coordinator.async_refresh(discover))

    assert coordinator.rooms == (refreshed_room,)
    assert set(coordinator.statuses) == {"bedroom"}
    assert "sensor.bedroom" in coordinator._tracked_source_ids


def test_covered_valve_gets_covered_cadence_and_its_mode_is_never_written() -> None:
    """Covered mode needs a refresh within 35 min; exposed mode allows 3 hours."""
    room = RoomDefinition(
        "office",
        "Büro",
        (
            ValveTarget("covered", "number.covered", "switch.w1", "switch.covered"),
            ValveTarget("exposed", "number.exposed", "switch.w2", "switch.exposed"),
        ),
        ("sensor.room",),
        (),
    )
    hass = FakeHass(
        {
            "sensor.room": state("sensor.room", "21.0", CELSIUS),
            "switch.covered": state("switch.covered", "on"),
            "switch.exposed": state("switch.exposed", "off"),
        }
    )
    from custom_components.danfoss_ally_coordinator.planner import SyncRecord

    last = SyncRecord(21.0, NOW - timedelta(minutes=26))
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        sync_records={"number.covered": last, "number.exposed": last},
    )

    evaluate(coordinator)

    assert written(hass) == ["number.covered"]
    assert coordinator.valve_diagnostics(room)[0]["mode"] == "covered"
    assert coordinator.valve_diagnostics(room)[1]["mode"] == "exposed"
    assert "switch.covered" in coordinator._tracked_source_ids


def test_unavailable_room_temperature_writes_nothing_and_keeps_mode() -> None:
    """No stale value is written and covered mode is never used as a fallback."""
    room = RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(
            ValveTarget(
                "trv-1",
                "number.trv_external_temperature",
                "switch.window",
                "switch.covered",
            ),
        ),
        temperature_entity_ids=("sensor.room",),
        window_entity_ids=(),
    )
    hass = FakeHass(
        {
            "sensor.room": state("sensor.room", "unavailable", CELSIUS),
            "switch.covered": state("switch.covered", "on"),
            "switch.window": state("switch.window", "off"),
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=True, max_sensor_age=timedelta(hours=2)
    )

    evaluate(coordinator)

    assert coordinator.statuses["office"].mean_temperature is None
    assert hass.states.get("switch.covered").state == "on"
    assert hass.services.calls == []


def test_failed_temperature_readback_is_not_recorded_as_success() -> None:
    """A service call that does not change the number state is retried later."""
    room = RoomDefinition(
        "office",
        "Office",
        (ValveTarget("trv", "number.temp", "switch.window"),),
        ("sensor.room",),
        (),
    )
    hass = FakeHass(
        {
            "sensor.room": state("sensor.room", "21", CELSIUS),
            "number.temp": state("number.temp", "20", CELSIUS),
        }
    )

    async def stale_number(domain, service, data, *, blocking):
        hass.services.calls.append((domain, service, data, blocking))

    hass.services.async_call = stale_number
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=True, max_sensor_age=timedelta(hours=2)
    )

    evaluate(coordinator)

    assert "readback mismatch" in coordinator.statuses["office"].last_error
    assert coordinator.sync_records == {}


def test_unknown_window_contacts_do_not_send_closed_signal() -> None:
    room = RoomDefinition(
        "office",
        "Office",
        (ValveTarget("trv", "number.temp", "switch.window"),),
        (),
        ("binary_sensor.window",),
    )
    hass = FakeHass(
        {
            "binary_sensor.window": state("binary_sensor.window", "unavailable"),
            "switch.window": state("switch.window", "on"),
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=True, max_sensor_age=timedelta(hours=2)
    )
    evaluate(coordinator)
    assert coordinator.statuses["office"].window_open is None
    assert coordinator.statuses["office"].unavailable_window_contacts == (
        "binary_sensor.window",
    )
    assert "switch.window" not in written(hass)


def test_one_dead_contact_does_not_keep_the_room_open_forever() -> None:
    """Available closed contacts close the room; the dead one is reported."""
    room = RoomDefinition(
        "office",
        "Office",
        (ValveTarget("trv", "number.temp", "switch.window"),),
        (),
        ("binary_sensor.dead", "binary_sensor.door"),
    )
    hass = FakeHass(
        {
            "binary_sensor.dead": state("binary_sensor.dead", "unavailable"),
            "binary_sensor.door": state("binary_sensor.door", "off"),
            "switch.window": state("switch.window", "on"),
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=True, max_sensor_age=timedelta(hours=2)
    )
    evaluate(coordinator)
    assert coordinator.statuses["office"].window_open is False
    assert coordinator.statuses["office"].unavailable_window_contacts == (
        "binary_sensor.dead",
    )
    assert hass.states.get("switch.window").state == "off"


def test_ignored_sources_are_listed_but_not_used() -> None:
    """Switched-off sources stay visible with status `ignored` and no influence."""
    room = RoomDefinition(
        "office",
        "Office",
        (ValveTarget("trv", "number.temp", "switch.window"),),
        ("sensor.room", "sensor.radiator_shelf", "sensor.old"),
        ("binary_sensor.phantom", "binary_sensor.door"),
    )
    hass = FakeHass(
        {
            "sensor.room": state("sensor.room", "21.0", CELSIUS),
            "sensor.radiator_shelf": state("sensor.radiator_shelf", "30.0", CELSIUS),
            "sensor.old": state(
                "sensor.old", "20.0", CELSIUS, updated=NOW - timedelta(hours=5)
            ),
            "binary_sensor.phantom": state("binary_sensor.phantom", "on"),
            "binary_sensor.door": state("binary_sensor.door", "off"),
            "switch.window": state("switch.window", "on"),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        excluded_entity_ids=frozenset(
            {"sensor.radiator_shelf", "binary_sensor.phantom"}
        ),
    )
    persisted: list[dict[str, list[str]]] = []
    coordinator.on_live_settings_changed = persisted.append

    evaluate(coordinator)

    assert coordinator.statuses["office"].mean_temperature == 21.0
    assert hass.states.get("switch.window").state == "off"
    details = coordinator.source_details(room)
    statuses = {item["entity_id"]: item["status"] for item in details["temperature"]}
    assert statuses["sensor.radiator_shelf"] == "ignored"
    assert statuses["sensor.room"] == "used"
    assert details["window"] == [
        {"entity_id": "binary_sensor.phantom", "status": "ignored"},
        {"entity_id": "binary_sensor.door", "status": "closed"},
    ]

    switch_and_settle(
        coordinator, coordinator.async_set_source_enabled("sensor.radiator_shelf", True)
    )

    assert persisted[-1]["excluded_entities"] == ["binary_sensor.phantom"]
    evaluate(coordinator)
    assert coordinator.statuses["office"].mean_temperature == 25.5


def _valve_window_room(*, contacts: tuple[str, ...] = ()) -> RoomDefinition:
    return RoomDefinition(
        "office",
        "Office",
        (
            ValveTarget(
                "one",
                "number.one",
                "switch.one",
                window_detection_entity_id="sensor.one",
            ),
            ValveTarget(
                "two",
                "number.two",
                "switch.two",
                window_detection_entity_id="sensor.two",
            ),
        ),
        (),
        contacts,
    )


def test_optional_valve_window_detection_propagates_only_local_open() -> None:
    room = _valve_window_room(contacts=("binary_sensor.contact",))
    hass = FakeHass(
        {
            "binary_sensor.contact": state("binary_sensor.contact", "off"),
            "sensor.one": state("sensor.one", "Open"),
            "sensor.two": state("sensor.two", "External"),
            "switch.one": state("switch.one", "off"),
            "switch.two": state("switch.two", "off"),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        sync_valve_windows=True,
        max_sensor_age=timedelta(hours=2),
    )
    evaluate(coordinator)
    assert hass.states.get("switch.two").state == "on"
    hass.states.set(state("sensor.one", "External"))
    evaluate(coordinator)
    assert hass.states.get("switch.two").state == "off"


def test_valve_window_external_echo_clears_without_room_contact() -> None:
    hass = FakeHass(
        {
            "sensor.one": state("sensor.one", "Open"),
            "sensor.two": state("sensor.two", "External"),
            "switch.one": state("switch.one", "off"),
            "switch.two": state("switch.two", "off"),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(_valve_window_room(),),
        enabled=True,
        sync_valve_windows=True,
        max_sensor_age=timedelta(hours=2),
    )
    evaluate(coordinator)
    assert hass.states.get("switch.two").state == "on"
    hass.states.set(state("sensor.one", "External"))
    evaluate(coordinator)
    assert hass.states.get("switch.one").state == "off"
    assert hass.states.get("switch.two").state == "off"


@pytest.mark.parametrize("report", ["Quarantine", "Maybe", "Closed"])
def test_valve_window_releases_on_any_not_locally_open_report(report: str) -> None:
    """Catalogue §3.3: values 0–2 and 4 mean the valve does not see an open window."""
    hass = FakeHass(
        {
            "sensor.one": state("sensor.one", report),
            "sensor.two": state("sensor.two", "External"),
            "switch.one": state("switch.one", "on"),
            "switch.two": state("switch.two", "on"),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(_valve_window_room(),),
        enabled=True,
        sync_valve_windows=True,
        max_sensor_age=timedelta(hours=2),
    )
    evaluate(coordinator)
    assert hass.states.get("switch.one").state == "off"
    assert hass.states.get("switch.two").state == "off"


def test_valve_window_unknown_report_keeps_signal() -> None:
    """An unavailable valve report is not interpreted as closed."""
    hass = FakeHass(
        {
            "sensor.one": state("sensor.one", "unavailable"),
            "sensor.two": state("sensor.two", "External"),
            "switch.one": state("switch.one", "on"),
            "switch.two": state("switch.two", "on"),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(_valve_window_room(),),
        enabled=True,
        sync_valve_windows=True,
        max_sensor_age=timedelta(hours=2),
    )
    evaluate(coordinator)
    assert hass.services.calls == []


def _sync_room() -> RoomDefinition:
    return RoomDefinition(
        "office",
        "Office",
        (
            ValveTarget(
                "one", "number.one", "switch.one", climate_entity_id="climate.one"
            ),
            ValveTarget(
                "two", "number.two", "switch.two", climate_entity_id="climate.two"
            ),
        ),
        (),
        (),
    )


def _sync_hass() -> FakeHass:
    return FakeHass(
        {
            "climate.one": state("climate.one", "heat", {"temperature": 21.0}),
            "climate.two": state("climate.two", "heat", {"temperature": 20.0}),
        }
    )


def _sync_coordinator(hass: FakeHass, **kwargs) -> RoomCoordinator:
    return RoomCoordinator(
        hass,
        rooms=(_sync_room(),),
        enabled=kwargs.pop("enabled", True),
        max_sensor_age=timedelta(hours=2),
        **kwargs,
    )


def _change(coordinator, entity_id, old, new, *, at=NOW, context=None) -> None:
    asyncio.run(
        coordinator.async_handle_setpoint_change(
            entity_id,
            state(entity_id, "heat", {"temperature": old}, updated=at),
            state(entity_id, "heat", {"temperature": new}, updated=at, context=context),
        )
    )


def test_setpoint_sync_is_on_by_default_and_ignores_its_own_echo() -> None:
    hass = _sync_hass()
    coordinator = _sync_coordinator(hass)
    assert coordinator.setpoint_sync_enabled(_sync_room())

    _change(coordinator, "climate.one", 20.0, 21.0)
    assert hass.states.get("climate.two").attributes["temperature"] == 21.0
    assert written(hass) == ["climate.two"]
    assert coordinator.statuses["office"].last_reason == "setpoint_sync"

    # The mirrored write's own state change is consumed once, never re-mirrored.
    _change(coordinator, "climate.two", 20.0, 21.0)
    assert written(hass) == ["climate.two"]
    assert coordinator._expected_setpoint_echoes == {}


@pytest.mark.parametrize("context", [None, Context(user_id="user")])
def test_setpoint_sync_follows_every_kind_of_change(context) -> None:
    """Dial, HA user, automation or valve schedule: the room follows."""
    hass = _sync_hass()
    coordinator = _sync_coordinator(hass)
    _change(coordinator, "climate.one", 20.0, 21.0, context=context)
    assert written(hass) == ["climate.two"]


def test_setpoint_sync_can_be_switched_off_per_room() -> None:
    hass = _sync_hass()
    coordinator = _sync_coordinator(hass)
    persisted: list[dict[str, list[str]]] = []
    coordinator.on_live_settings_changed = persisted.append

    switch_and_settle(coordinator, coordinator.async_set_setpoint_sync("office", False))
    _change(coordinator, "climate.one", 20.0, 21.0)

    assert hass.services.calls == []
    assert persisted[-1]["setpoint_sync_disabled_areas"] == ["office"]
    assert not coordinator.setpoint_sync_enabled(_sync_room())


def test_setpoint_sync_needs_master_synchronization() -> None:
    hass = _sync_hass()
    coordinator = _sync_coordinator(hass, enabled=False)
    _change(coordinator, "climate.one", 20.0, 21.0)
    assert hass.services.calls == []


def test_setpoint_sync_waits_for_the_dial_to_settle(monkeypatch) -> None:
    """Intermediate dial positions are skipped; only the final target is sent."""
    from custom_components.danfoss_ally_coordinator import coordinator as module

    monkeypatch.setattr(module, "SETPOINT_SETTLE_DELAY", 0)
    hass = _sync_hass()
    hass.states.set(state("climate.one", "heat", {"temperature": 22.0}))
    coordinator = _sync_coordinator(hass)

    asyncio.run(
        coordinator._async_delayed_setpoint_change(
            "climate.one",
            state("climate.one", "heat", {"temperature": 20.0}),
            state("climate.one", "heat", {"temperature": 21.0}),
        )
    )
    assert hass.services.calls == []

    asyncio.run(
        coordinator._async_delayed_setpoint_change(
            "climate.one",
            state("climate.one", "heat", {"temperature": 21.0}),
            state("climate.one", "heat", {"temperature": 22.0}),
        )
    )
    assert hass.states.get("climate.two").attributes["temperature"] == 22.0


def test_setpoint_sync_late_echo_after_readback_mismatch_is_suppressed() -> None:
    """A valve applying a mirrored setpoint late does not start a new round."""
    hass = _sync_hass()

    async def slow_valve(domain, service, data, *, blocking):
        hass.services.calls.append((domain, service, data, blocking))

    hass.services.async_call = slow_valve
    coordinator = _sync_coordinator(hass)

    _change(coordinator, "climate.one", 20.0, 21.0)
    assert "readback mismatch" in coordinator.statuses["office"].last_error

    _change(coordinator, "climate.two", 20.0, 21.0)
    assert written(hass) == ["climate.two"]


def test_setpoint_sync_failed_call_clears_echo_expectation() -> None:
    hass = _sync_hass()

    async def failing(domain, service, data, *, blocking):
        raise HomeAssistantError("radio busy")

    hass.services.async_call = failing
    coordinator = _sync_coordinator(hass)
    _change(coordinator, "climate.one", 20.0, 21.0)
    assert "radio busy" in coordinator.statuses["office"].last_error
    assert coordinator._expected_setpoint_echoes == {}


def _load_valve(name: str) -> ValveTarget:
    return ValveTarget(
        name,
        f"number.{name}_external",
        f"switch.{name}_window",
        climate_entity_id=f"climate.{name}",
        load_estimate_entity_id=f"sensor.{name}_load",
        load_room_mean_entity_id=f"number.{name}_load_mean",
        load_balancing_enable_entity_id=f"switch.{name}_load_enable",
    )


def _load_setup(loads: dict[str, str], *, areas=frozenset({"office"})):
    room = RoomDefinition(
        "office", "Office", tuple(_load_valve(name) for name in loads), (), ()
    )
    values = {}
    for name, load in loads.items():
        values[f"sensor.{name}_load"] = state(f"sensor.{name}_load", load)
        values[f"number.{name}_load_mean"] = state(f"number.{name}_load_mean", "-8000")
        values[f"switch.{name}_load_enable"] = state(
            f"switch.{name}_load_enable", "off"
        )
    hass = FakeHass(values)
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        load_balancing_areas=areas,
    )
    return room, hass, coordinator


def test_load_balancing_sends_room_mean_of_valid_estimates() -> None:
    """Catalogue §2.2: -8000 and values below -500 are left out of the mean."""
    room, hass, coordinator = _load_setup({"a": "1000", "b": "1500", "c": "-8000"})

    evaluate(coordinator)

    assert coordinator.statuses["office"].room_load_mean == 1250
    for name in "abc":
        assert hass.states.get(f"switch.{name}_load_enable").state == "on"
        assert hass.states.get(f"number.{name}_load_mean").state == "1250"
    assert coordinator.load_diagnostics(room)["room_load_mean"] == 1250


def test_load_balancing_repeats_every_15_minutes_only() -> None:
    room, hass, coordinator = _load_setup({"a": "1000", "b": "1200"})
    evaluate(coordinator)
    calls = len(hass.services.calls)

    later = NOW + timedelta(minutes=10)
    asyncio.run(coordinator.async_evaluate_all(now=later, reason="cadence"))
    assert len(hass.services.calls) == calls

    later = NOW + timedelta(minutes=15)
    asyncio.run(coordinator.async_evaluate_all(now=later, reason="cadence"))
    assert written(hass)[calls:] == ["number.a_load_mean", "number.b_load_mean"]


def test_load_balancing_ignores_estimates_older_than_90_minutes() -> None:
    room, hass, coordinator = _load_setup({"a": "1000", "b": "1600"})
    hass.states.set(state("sensor.b_load", "1600", updated=NOW - timedelta(minutes=91)))
    evaluate(coordinator)
    assert coordinator.statuses["office"].room_load_mean == 1000


def test_load_balancing_is_off_by_default_and_needs_two_valves() -> None:
    _, hass, coordinator = _load_setup({"a": "1000", "b": "1500"}, areas=frozenset())
    evaluate(coordinator)
    assert hass.services.calls == []

    room, hass, coordinator = _load_setup({"a": "1000"})
    assert not coordinator.load_balancing_supported(room)
    evaluate(coordinator)
    assert hass.services.calls == []


def test_switching_load_balancing_off_disables_it_on_the_valves() -> None:
    room, hass, coordinator = _load_setup({"a": "1000", "b": "1500"})
    coordinator._started = True
    persisted: list[dict[str, list[str]]] = []
    coordinator.on_live_settings_changed = persisted.append
    evaluate(coordinator)
    assert hass.states.get("number.a_load_mean").state == "1250"

    switch_and_settle(
        coordinator, coordinator.async_set_load_balancing("office", False)
    )

    assert hass.states.get("number.a_load_mean").state == "-8000"
    assert hass.states.get("number.b_load_mean").state == "-8000"
    assert hass.states.get("switch.a_load_enable").state == "off"
    assert hass.states.get("switch.b_load_enable").state == "off"
    assert persisted[-1]["load_balancing_areas"] == []
    assert not coordinator.load_balancing_active(room)


def test_single_valve_room_automatically_turns_off_saved_load_balancing() -> None:
    room, hass, coordinator = _load_setup({"a": "1000"})
    hass.states.set(state("number.a_load_mean", "1250"))
    hass.states.set(state("switch.a_load_enable", "on"))
    coordinator._started = True
    persisted = []
    coordinator.on_live_settings_changed = persisted.append

    evaluate(coordinator)

    assert coordinator.load_balancing_areas == frozenset()
    assert persisted[-1]["load_balancing_areas"] == []
    assert hass.states.get("number.a_load_mean").state == "-8000"
    assert hass.states.get("switch.a_load_enable").state == "off"
    assert not coordinator.load_balancing_active(room)


def test_load_balancing_turns_off_after_refresh_leaves_one_valve() -> None:
    room, hass, coordinator = _load_setup({"a": "1000", "b": "1500"})
    coordinator._started = True
    persisted = []
    coordinator.on_live_settings_changed = persisted.append
    evaluate(coordinator)
    one_valve_room = RoomDefinition("office", "Office", (room.valves[0],), (), ())

    async def discover():
        return (one_valve_room,)

    asyncio.run(coordinator.async_refresh(discover))

    assert coordinator.load_balancing_areas == frozenset()
    assert persisted[-1]["load_balancing_areas"] == []
    assert hass.states.get("number.a_load_mean").state == "-8000"
    assert hass.states.get("switch.a_load_enable").state == "off"


def test_single_valve_room_clears_device_load_mode_without_room_opt_in() -> None:
    _, hass, coordinator = _load_setup({"a": "1000"}, areas=frozenset())
    hass.states.set(state("number.a_load_mean", "1250"))
    hass.states.set(state("switch.a_load_enable", "on"))
    coordinator._started = True

    evaluate(coordinator)

    assert hass.states.get("number.a_load_mean").state == "-8000"
    assert hass.states.get("switch.a_load_enable").state == "off"
    calls = len(hass.services.calls)
    evaluate(coordinator)
    assert len(hass.services.calls) == calls


def test_cannot_enable_load_balancing_for_one_valve_room() -> None:
    _, hass, coordinator = _load_setup({"a": "1000"}, areas=frozenset())
    coordinator._started = True
    persisted = []
    coordinator.on_live_settings_changed = persisted.append

    switch_and_settle(coordinator, coordinator.async_set_load_balancing("office", True))

    assert coordinator.load_balancing_areas == frozenset()
    assert persisted == []
    assert hass.services.calls == []


def _calibration_room(*, covered: str | None = None) -> RoomDefinition:
    return RoomDefinition(
        "office",
        "Office",
        (
            ValveTarget(
                "one",
                "number.one",
                "switch.one",
                "switch.covered" if covered else None,
                climate_entity_id="climate.one",
                calibration_entity_id="number.offset_one",
            ),
            ValveTarget(
                "two",
                "number.two",
                "switch.two",
                climate_entity_id="climate.two",
                calibration_entity_id="number.offset_two",
            ),
        ),
        ("sensor.room",),
        (),
    )


def _calibration_hass(
    *, room_age: timedelta = timedelta(0), covered: str | None = None
) -> FakeHass:
    offset = {"unit_of_measurement": "°C", "min": -2.5, "max": 2.5}
    values = {
        "sensor.room": state("sensor.room", "21.8", CELSIUS, updated=NOW - room_age),
        "climate.one": state("climate.one", "heat", {"current_temperature": 21.0}),
        "number.offset_one": state("number.offset_one", "0.0", offset),
        "number.offset_two": state("number.offset_two", "0.0", offset),
    }
    if covered:
        values["switch.covered"] = state("switch.covered", covered)
    return FakeHass(values)


def _observe_live_valve(coordinator: RoomCoordinator, hass: FakeHass) -> None:
    """Simulate the valve reporting a changed local temperature."""
    coordinator.async_observe_valve_state(
        "climate.one",
        state("climate.one", "heat", {"current_temperature": 20.9}),
        hass.states.get("climate.one"),
    )


def test_calibration_is_explicit_and_writes_only_selected_valve() -> None:
    hass = _calibration_hass()
    coordinator = RoomCoordinator(
        hass,
        rooms=(_calibration_room(),),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )
    _observe_live_valve(coordinator, hass)
    result = asyncio.run(
        coordinator.async_calibrate_local_sensor("climate.one", now=NOW)
    )
    assert result["previous_offset"] == 0.0
    assert result["offset"] == 0.8
    assert hass.states.get("number.offset_one").state == "0.8"
    assert hass.states.get("number.offset_two").state == "0.0"
    assert written(hass) == ["number.offset_one"]


def test_calibration_respects_the_number_entity_limits() -> None:
    hass = _calibration_hass()
    hass.states.set(
        state(
            "number.offset_one",
            "0.0",
            {"unit_of_measurement": "°C", "min": -0.5, "max": 0.5},
        )
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(_calibration_room(),),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )
    _observe_live_valve(coordinator, hass)
    result = asyncio.run(
        coordinator.async_calibrate_local_sensor("climate.one", now=NOW)
    )
    assert result["offset"] == 0.5


def test_calibration_refuses_stale_room_temperature() -> None:
    hass = _calibration_hass(room_age=timedelta(hours=3))
    coordinator = RoomCoordinator(
        hass,
        rooms=(_calibration_room(),),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )
    _observe_live_valve(coordinator, hass)
    with pytest.raises(HomeAssistantError, match="room temperature"):
        asyncio.run(coordinator.async_calibrate_local_sensor("climate.one", now=NOW))
    assert hass.services.calls == []


def test_calibration_refuses_valve_reading_not_proven_fresh() -> None:
    """A recently refreshed climate state alone does not prove the reading is new."""
    hass = _calibration_hass()
    coordinator = RoomCoordinator(
        hass,
        rooms=(_calibration_room(),),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )
    # Another attribute changed; current_temperature stayed the same.
    coordinator.async_observe_valve_state(
        "climate.one",
        state("climate.one", "heat", {"current_temperature": 21.0, "x": 1}),
        hass.states.get("climate.one"),
    )
    with pytest.raises(HomeAssistantError, match="changed temperature"):
        asyncio.run(coordinator.async_calibrate_local_sensor("climate.one", now=NOW))
    assert hass.services.calls == []


def test_calibration_refuses_covered_radiator_mode() -> None:
    hass = _calibration_hass(covered="on")
    coordinator = RoomCoordinator(
        hass,
        rooms=(_calibration_room(covered="on"),),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )
    _observe_live_valve(coordinator, hass)
    with pytest.raises(HomeAssistantError, match="covered-radiator"):
        asyncio.run(coordinator.async_calibrate_local_sensor("climate.one", now=NOW))
    assert hass.services.calls == []


def test_local_temperature_gap_is_read_only_per_valve() -> None:
    hass = _calibration_hass()
    room = _calibration_room()
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=False, max_sensor_age=timedelta(hours=2)
    )
    evaluate(coordinator)
    entity = LocalTemperatureGapSensor(coordinator, room, room.valves[0])
    assert "one" in entity.unique_id
    # Unknown until the valve has demonstrably reported a new reading.
    assert entity.native_value is None
    _observe_live_valve(coordinator, hass)
    assert entity.native_value == 0.8
    assert hass.services.calls == []


def test_room_switch_applies_at_once_while_slow_valves_are_written() -> None:
    """A battery valve may take minutes to answer; the switch must not wait."""
    room, hass, coordinator = _load_setup({"a": "1000", "b": "1500"}, areas=frozenset())
    coordinator._started = True
    # The background evaluation runs at wall-clock time.
    for name, load in (("a", "1000"), ("b", "1500")):
        hass.states.set(state(f"sensor.{name}_load", load, updated=datetime.now(UTC)))
    release = asyncio.Event()
    original = hass.services.async_call

    async def slow(domain, service, data, *, blocking):
        await release.wait()
        await original(domain, service, data, blocking=blocking)

    hass.services.async_call = slow

    async def run() -> None:
        await coordinator.async_set_load_balancing("office", True)
        assert coordinator.load_balancing_active(room)
        assert hass.services.calls == []
        release.set()
        await asyncio.gather(*coordinator._tasks)

    asyncio.run(run())
    assert hass.states.get("number.a_load_mean").state == "1250"


def test_queued_evaluations_are_coalesced_behind_a_slow_write() -> None:
    hass = FakeHass({})
    coordinator = RoomCoordinator(
        hass, rooms=(), enabled=False, max_sensor_age=timedelta(hours=2)
    )
    runs: list[str] = []

    async def record(*, now, reason, allow_writes=True):
        runs.append(reason)

    coordinator.async_evaluate_all = record

    async def run() -> None:
        await coordinator._evaluation_lock.acquire()  # a slow write in progress
        waiting = [
            asyncio.create_task(coordinator._async_evaluate_serial(reason=f"e{i}"))
            for i in range(5)
        ]
        await asyncio.sleep(0)
        coordinator._evaluation_lock.release()
        await asyncio.gather(*waiting)

    asyncio.run(run())
    assert runs == ["e0"]


def test_offline_valve_is_skipped_and_retried_later() -> None:
    """While ZHA cannot reach a valve (e.g. after a restart) nothing is written."""
    room = RoomDefinition(
        "office",
        "Office",
        (
            ValveTarget(
                "one", "number.one", "switch.one", climate_entity_id="climate.one"
            ),
        ),
        ("sensor.room",),
        ("binary_sensor.door",),
    )
    hass = FakeHass(
        {
            "sensor.room": state("sensor.room", "21.0", CELSIUS),
            "binary_sensor.door": state("binary_sensor.door", "on"),
            "climate.one": state("climate.one", "unavailable"),
            "switch.one": state("switch.one", "unavailable"),
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=True, max_sensor_age=timedelta(hours=2)
    )

    evaluate(coordinator)

    assert hass.services.calls == []
    assert coordinator.statuses["office"].offline_valves == ("climate.one",)
    assert coordinator.valve_diagnostics(room)[0]["online"] is False
    assert coordinator.sync_records == {}

    hass.states.set(state("climate.one", "heat", {"temperature": 20.0}))
    evaluate(coordinator)
    assert written(hass) == ["number.one", "switch.one"]


def test_sources_count_again_only_after_reporting_since_restart() -> None:
    """Cached values restored at start-up look fresh but prove nothing."""
    room = RoomDefinition(
        "office",
        "Office",
        (ValveTarget("trv", "number.trv", "switch.trv"),),
        ("sensor.restored", "sensor.reported"),
        (),
    )
    started = NOW - timedelta(minutes=10)
    hass = FakeHass(
        {
            "sensor.restored": state(
                "sensor.restored",
                "30.0",
                CELSIUS,
                updated=started - timedelta(seconds=5),
            ),
            "sensor.reported": state(
                "sensor.reported",
                "21.0",
                CELSIUS,
                updated=started + timedelta(minutes=2),
            ),
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=False, max_sensor_age=timedelta(hours=2)
    )
    coordinator._ha_started_at = started

    evaluate(coordinator)

    assert coordinator.statuses["office"].mean_temperature == 21.0
    statuses = {
        item["entity_id"]: item["status"]
        for item in coordinator.source_details(room)["temperature"]
    }
    assert statuses == {
        "sensor.restored": "waiting_since_restart",
        "sensor.reported": "used",
    }


def test_setpoint_sync_respects_each_valves_own_limits() -> None:
    """A peer limited to 22 °C gets 22 °C and does not pull the room back down."""
    hass = _sync_hass()
    hass.states.set(
        state("climate.two", "heat", {"temperature": 20.0, "max_temp": 22.0})
    )
    coordinator = _sync_coordinator(hass)

    _change(coordinator, "climate.one", 21.0, 23.0)
    assert hass.states.get("climate.two").attributes["temperature"] == 22.0

    # The limited value is the expected echo, not a new change for the room.
    _change(coordinator, "climate.two", 20.0, 22.0)
    assert written(hass) == ["climate.two"]


def test_setpoint_sync_does_not_interrupt_an_adaptation_run() -> None:
    """Catalogue §1.6: a user-interaction setpoint would cancel the run."""
    room = RoomDefinition(
        "office",
        "Office",
        (
            ValveTarget(
                "one", "number.one", "switch.one", climate_entity_id="climate.one"
            ),
            ValveTarget(
                "two",
                "number.two",
                "switch.two",
                climate_entity_id="climate.two",
                adaptation_status_entity_id="sensor.two_adaptation",
            ),
        ),
        (),
        (),
    )
    hass = _sync_hass()
    hass.states.set(state("sensor.two_adaptation", "something", {"In_progress": True}))
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=True, max_sensor_age=timedelta(hours=2)
    )
    _change(coordinator, "climate.one", 20.0, 21.0)
    assert hass.services.calls == []


def test_valve_errors_are_readable_from_the_built_in_quirk() -> None:
    valve = ValveTarget("one", "number.one", "switch.one", error_entity_id="sensor.err")
    room = RoomDefinition("office", "Office", (valve,), (), ())
    hass = FakeHass(
        {
            "sensor.err": state(
                "sensor.err",
                "something",
                {
                    "Invalid_clock_information": True,
                    "Low_battery": False,
                    "friendly_name": "Software error",
                },
            )
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(room,), enabled=False, max_sensor_age=timedelta(hours=2)
    )
    assert coordinator.valve_diagnostics(room)[0]["errors"] == [
        "Invalid_clock_information"
    ]


def heat_room() -> RoomDefinition:
    """One valve that exposes catalogue §2.7 HeatAvailable."""
    return RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(
            ValveTarget(
                "trv-1",
                "number.trv",
                "switch.window",
                heat_available_entity_id="switch.trv_heat_available",
            ),
        ),
        temperature_entity_ids=(),
        window_entity_ids=(),
    )


def test_heat_available_is_forwarded_on_change_and_refreshed() -> None:
    """The heat source reaches every valve; an unchanged value is re-sent later."""
    hass = FakeHass(
        {
            "binary_sensor.ecl_heat": state("binary_sensor.ecl_heat", "on"),
            "switch.trv_heat_available": state("switch.trv_heat_available", "off"),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(heat_room(),),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        heat_source_entity_id="binary_sensor.ecl_heat",
    )
    assert "binary_sensor.ecl_heat" in coordinator._tracked_source_ids
    assert "binary_sensor.ecl_heat" in coordinator.known_entity_ids()

    evaluate(coordinator)
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.trv_heat_available"}, True)
    ]

    # Unchanged and recently confirmed: no radio write.
    evaluate(coordinator)
    assert len(hass.services.calls) == 1

    # The system stops supplying heat: the valve is told at once.
    hass.states.set(state("binary_sensor.ecl_heat", "off"))
    evaluate(coordinator)
    assert hass.services.calls[-1][1] == "turn_off"

    # Unchanged, but old enough: re-sent so a valve that missed it learns again.
    asyncio.run(
        coordinator.async_evaluate_all(now=NOW + timedelta(hours=3), reason="test")
    )
    assert hass.services.calls[-1][1] == "turn_off"
    assert len(hass.services.calls) == 3


@pytest.mark.parametrize("source_state", [None, "unavailable", "unknown"])
def test_heat_available_needs_a_known_source(source_state: str | None) -> None:
    """Without a configured or readable source the valve setting stays untouched."""
    values = {"switch.trv_heat_available": state("switch.trv_heat_available", "off")}
    if source_state:
        values["binary_sensor.ecl_heat"] = state("binary_sensor.ecl_heat", source_state)
    hass = FakeHass(values)
    coordinator = RoomCoordinator(
        hass,
        rooms=(heat_room(),),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        heat_source_entity_id="binary_sensor.ecl_heat" if source_state else None,
    )
    evaluate(coordinator)
    assert hass.services.calls == []


def upkeep_room() -> RoomDefinition:
    """One valve with every entity the valve upkeep uses."""
    return RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(
            ValveTarget(
                "trv-1",
                "number.trv",
                "switch.window",
                error_entity_id="sensor.trv_error",
                adaptation_status_entity_id="sensor.trv_adaptation",
                adaptation_control_entity_id="select.trv_adaptation_control",
                adaptation_settings_entity_id="switch.trv_adaptation_nightly",
                exercise_day_entity_id="select.trv_exercise_day",
                exercise_time_entity_id="number.trv_exercise_time",
                heat_required_entity_id="binary_sensor.trv_heat_required",
                pi_demand_entity_id="sensor.trv_pi_demand",
                ieee="00:11:22:33:44:55:66:77",
            ),
        ),
        temperature_entity_ids=(),
        window_entity_ids=(),
    )


def upkeep_hass(**overrides: State) -> FakeHass:
    values = {
        "sensor.trv_error": state("sensor.trv_error", "nothing"),
        "sensor.trv_adaptation": state(
            "sensor.trv_adaptation",
            "nothing",
            {"In_progress": False, "Valve_characteristic_found": False},
        ),
        "select.trv_adaptation_control": state(
            "select.trv_adaptation_control", "Nothing"
        ),
        "switch.trv_adaptation_nightly": state("switch.trv_adaptation_nightly", "on"),
        "select.trv_exercise_day": state("select.trv_exercise_day", "Wednesday"),
        "number.trv_exercise_time": state("number.trv_exercise_time", "600"),
        "binary_sensor.ecl_heat": state("binary_sensor.ecl_heat", "off"),
    }
    values.update({entity_id: value for entity_id, value in overrides.items()})
    return FakeHass(values)


def run_upkeep(coordinator: RoomCoordinator, now: datetime = NOW) -> None:
    async def run() -> None:
        coordinator._started = True
        await coordinator.async_evaluate_all(now=now, reason="test")
        await asyncio.gather(*coordinator._tasks)

    asyncio.run(run())


def upkeep_calls(hass: FakeHass) -> list[tuple[str, str, object]]:
    return [
        (domain, service, data.get("option", data.get("value", data.get("attribute"))))
        for domain, service, data, _ in hass.services.calls
    ]


def test_a_lost_clock_is_set_with_all_six_time_attributes() -> None:
    """Catalogue §1.2: time, status, zone and DST, at once when the clock is lost."""
    hass = upkeep_hass(
        **{
            "sensor.trv_error": state(
                "sensor.trv_error", "something", {"Invalid_clock_information": True}
            )
        }
    )
    coordinator = RoomCoordinator(
        hass, rooms=(upkeep_room(),), enabled=True, max_sensor_age=timedelta(hours=2)
    )
    run_upkeep(coordinator)

    zha = [call for call in hass.services.calls if call[0] == "zha"]
    assert [call[2]["attribute"] for call in zha] == [0, 1, 2, 3, 4, 5]
    assert all(call[2]["cluster_id"] == 0x000A for call in zha)
    assert all(call[2]["ieee"] == "00:11:22:33:44:55:66:77" for call in zha)
    values = {call[2]["attribute"]: call[2]["value"] for call in zha}
    assert values[1] == 0b10 and values[2] == 3600 and values[5] == 3600
    assert "time:trv-1" in coordinator.sync_records

    # Done: not repeated within the week (unless the clock is lost again later).
    hass.states.set(state("sensor.trv_error", "nothing"))
    hass.services.calls.clear()
    run_upkeep(coordinator, NOW + timedelta(days=1))
    assert not [call for call in hass.services.calls if call[0] == "zha"]


def test_the_exercise_is_staggered_on_the_configured_day() -> None:
    """Catalogue §3.8: one day for all valves, start time plus 10 min per valve."""
    hass = upkeep_hass()
    coordinator = RoomCoordinator(
        hass,
        rooms=(upkeep_room(),),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        exercise_day="Thursday",
        exercise_time=time(11, 0),
    )
    run_upkeep(coordinator)
    calls = upkeep_calls(hass)
    assert ("select", "select_option", "Thursday") in calls
    assert ("number", "set_value", 660) in calls

    # Matching the plan: nothing is written again.
    hass.services.calls.clear()
    run_upkeep(coordinator, NOW + timedelta(hours=2))
    assert not [c for c in upkeep_calls(hass) if c[0] in ("select", "number")]


def test_adaptation_runs_only_after_heat_has_been_available() -> None:
    """Catalogue §1.6: nightly run off; start once heat was available for 30 min."""
    hass = upkeep_hass(
        **{"binary_sensor.ecl_heat": state("binary_sensor.ecl_heat", "on")}
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(upkeep_room(),),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        heat_source_entity_id="binary_sensor.ecl_heat",
        adaptation_with_heat=True,
    )
    run_upkeep(coordinator)
    calls = upkeep_calls(hass)
    assert ("switch", "turn_off", None) in [
        (d, s, None) for d, s, _ in calls if d == "switch"
    ]
    assert ("select", "select_option", "Initiate") not in calls  # heat just arrived

    run_upkeep(coordinator, NOW + timedelta(minutes=31))
    assert ("select", "select_option", "Initiate") in upkeep_calls(hass)

    # A valve that already knows its characteristic is left alone.
    hass.services.calls.clear()
    hass.states.set(
        state(
            "sensor.trv_adaptation",
            "something",
            {"In_progress": False, "Valve_characteristic_found": True},
        )
    )
    run_upkeep(coordinator, NOW + timedelta(days=2))
    assert ("select", "select_option", "Initiate") not in upkeep_calls(hass)


def test_house_demand_combines_valves_and_floor_heating() -> None:
    """Highest demand, any room needing heat, and which rooms."""
    hass = upkeep_hass(
        **{
            "binary_sensor.trv_heat_required": state(
                "binary_sensor.trv_heat_required", "on"
            ),
            "sensor.trv_pi_demand": state("sensor.trv_pi_demand", "35"),
            "climate.bath_floor": state(
                "climate.bath_floor", "heat", {"hvac_action": "idle"}
            ),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(upkeep_room(),),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
        demand_climates={"climate.bath_floor": "Bad"},
    )
    demand = coordinator.house_demand()
    assert (
        demand.max_percent == 35 and demand.any_required and demand.rooms == ("Büro",)
    )

    hass.states.set(state("climate.bath_floor", "heat", {"hvac_action": "heating"}))
    demand = coordinator.house_demand()
    assert demand.max_percent == 100 and demand.rooms == ("Bad", "Büro")
    assert "climate.bath_floor" in coordinator.demand_entity_ids()
