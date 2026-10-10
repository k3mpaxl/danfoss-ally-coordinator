"""End-to-end coordinator behavior using only typed existing ZHA entities."""

import asyncio
from datetime import UTC, datetime, timedelta

from homeassistant.core import State

from custom_components.danfoss_ally_coordinator.coordinator import RoomCoordinator
from custom_components.danfoss_ally_coordinator.discovery import (
    RoomDefinition,
    ValveTarget,
)
from custom_components.danfoss_ally_coordinator.store import SyncStateStore


class FakeStates:
    """Small state registry compatible with the coordinator's read-only use."""

    def __init__(self, values: dict[str, State]) -> None:
        self._values = values

    def get(self, entity_id: str) -> State | None:
        return self._values.get(entity_id)


class FakeServices:
    """Records typed HA service calls without contacting a live device."""

    def __init__(self, states: FakeStates) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict[str, object], bool]] = []

    async def async_call(
        self, domain: str, service: str, data: dict[str, object], *, blocking: bool
    ) -> None:
        self.calls.append((domain, service, data, blocking))
        entity_id = data["entity_id"]
        if domain == "number" and service == "set_value":
            old = self.states.get(entity_id)
            self.states._values[entity_id] = State(
                entity_id,
                str(data["value"]),
                old.attributes if old is not None else {"unit_of_measurement": "°C"},
            )
        elif domain == "switch":
            old = self.states.get(entity_id)
            self.states._values[entity_id] = State(
                entity_id,
                "on" if service == "turn_on" else "off",
                old.attributes if old is not None else {},
            )


class FakeHass:
    """The minimal HA surface used for one evaluation."""

    def __init__(self, values: dict[str, State]) -> None:
        self.states = FakeStates(values)
        self.services = FakeServices(self.states)


def test_enabled_coordinator_averages_room_temperature_and_syncs_window_state() -> None:
    """One selected room writes its mean and any-open window state to each eTRV."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
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
            "sensor.a": State("sensor.a", "20.0", {"unit_of_measurement": "°C"}, now),
            "sensor.b": State("sensor.b", "22.0", {"unit_of_measurement": "°C"}, now),
            "binary_sensor.window": State("binary_sensor.window", "on", {}, now),
            "switch.trv_covered": State("switch.trv_covered", "off", {}, now),
            "switch.trv_external_window": State(
                "switch.trv_external_window", "off", {}, now
            ),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        max_sensor_age=timedelta(hours=2),
    )

    asyncio.run(coordinator.async_evaluate_all(now=now, reason="test"))

    assert coordinator.statuses["office"].mean_temperature == 21.0
    assert coordinator.statuses["office"].window_open
    assert hass.services.calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.trv_external_temperature", "value": 21.0},
            True,
        ),
        (
            "switch",
            "turn_on",
            {"entity_id": "switch.trv_external_window"},
            True,
        ),
    ]


def test_feature_toggles_can_disable_temperature_and_window_writes() -> None:
    """The master opt-in can keep each individual radio feature disabled."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    room = RoomDefinition(
        "office",
        "Büro",
        (ValveTarget("trv-1", "number.trv", "switch.window", None),),
        ("sensor.room",),
        ("binary_sensor.window",),
    )
    hass = FakeHass(
        {
            "sensor.room": State(
                "sensor.room", "21.0", {"unit_of_measurement": "°C"}, now
            ),
            "binary_sensor.window": State("binary_sensor.window", "on", {}, now),
            "number.trv": State(
                "number.trv", "20.0", {"unit_of_measurement": "°C"}, now
            ),
            "switch.window": State("switch.window", "off", {}, now),
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

    asyncio.run(coordinator.async_evaluate_all(now=now, reason="test"))

    assert hass.services.calls == []


def test_source_count_matches_only_readings_used_by_the_mean() -> None:
    """Diagnostics do not claim invalid/no-unit values contributed to a mean."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    room = RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(ValveTarget("trv-1", "number.trv", "switch.trv_window", None),),
        temperature_entity_ids=(
            "sensor.good",
            "sensor.no_unit",
            "sensor.nan",
            "sensor.old",
        ),
        window_entity_ids=(),
    )
    old_state = State("sensor.old", "22.0", {"unit_of_measurement": "°C"}, now)
    old_state.last_updated = now - timedelta(hours=3)
    coordinator = RoomCoordinator(
        FakeHass(
            {
                "sensor.good": State(
                    "sensor.good", "20.0", {"unit_of_measurement": "°C"}, now
                ),
                "sensor.no_unit": State("sensor.no_unit", "21.0", {}, now),
                "sensor.nan": State(
                    "sensor.nan", "nan", {"unit_of_measurement": "°C"}, now
                ),
                "sensor.old": old_state,
                "switch.trv_window": State("switch.trv_window", "off", {}, now),
            }
        ),
        rooms=(room,),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )

    asyncio.run(coordinator.async_evaluate_all(now=now, reason="test"))

    assert coordinator.statuses["office"].mean_temperature == 20.0
    assert coordinator.statuses["office"].valid_temperature_sources == 1


def test_successful_temperature_sync_is_persisted_for_the_next_restart() -> None:
    """A restart retains the last value/time and does not reset the radio cadence."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    room = RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(
            ValveTarget(
                "trv-1", "number.trv_external_temperature", "switch.trv_window", None
            ),
        ),
        temperature_entity_ids=("sensor.room",),
        window_entity_ids=(),
    )
    hass = FakeHass(
        {
            "sensor.room": State(
                "sensor.room", "21.0", {"unit_of_measurement": "°C"}, now
            ),
            "switch.trv_window": State("switch.trv_window", "off", {}, now),
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

    asyncio.run(coordinator.async_evaluate_all(now=now, reason="test"))

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
        valves=(ValveTarget("trv-1", "number.a", "switch.a", None),),
        temperature_entity_ids=("sensor.office",),
        window_entity_ids=(),
    )
    refreshed_room = RoomDefinition(
        area_id="bedroom",
        name="Schlafzimmer",
        valves=(ValveTarget("trv-2", "number.b", "switch.b", None),),
        temperature_entity_ids=("sensor.bedroom",),
        window_entity_ids=("binary_sensor.bedroom_window",),
    )
    coordinator = RoomCoordinator(
        FakeHass({}),
        rooms=(initial_room,),
        enabled=False,
        max_sensor_age=timedelta(hours=2),
    )

    coordinator.async_replace_rooms((refreshed_room,))

    assert coordinator.rooms == (refreshed_room,)
    assert set(coordinator.statuses) == {"bedroom"}


def test_external_sensor_priority_falls_back_when_all_sources_unavailable() -> None:
    """Unavailable room temperatures release external-sensor priority safely."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    room = RoomDefinition(
        area_id="office",
        name="Büro",
        valves=(
            ValveTarget(
                "trv-1",
                "number.trv_external_temperature",
                "switch.window",
                "switch.priority",
            ),
        ),
        temperature_entity_ids=("sensor.room",),
        window_entity_ids=(),
    )
    hass = FakeHass(
        {
            "sensor.room": State(
                "sensor.room", "unavailable", {"unit_of_measurement": "°C"}, now
            ),
            "switch.priority": State("switch.priority", "on", {}, now),
            "switch.window": State("switch.window", "off", {}, now),
        }
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        prioritize_external_sensor=True,
        max_sensor_age=timedelta(hours=2),
    )

    asyncio.run(coordinator.async_evaluate_all(now=now, reason="source_unavailable"))

    assert coordinator.statuses["office"].mean_temperature is None
    assert hass.states.get("switch.priority").state == "off"
    assert not [call for call in hass.services.calls if call[0] == "number"]


def test_external_sensor_priority_requires_readback() -> None:
    """A priority write is not successful until the switch state confirms it."""
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    room = RoomDefinition(
        "office",
        "Büro",
        (ValveTarget("trv-1", "number.trv", "switch.window", "switch.priority"),),
        ("sensor.room",),
        (),
    )
    hass = FakeHass(
        {
            "sensor.room": State(
                "sensor.room", "21.0", {"unit_of_measurement": "°C"}, now
            ),
            "number.trv": State(
                "number.trv", "20.0", {"unit_of_measurement": "°C"}, now
            ),
            "switch.window": State("switch.window", "off", {}, now),
            "switch.priority": State("switch.priority", "off", {}, now),
        }
    )

    async def no_priority_readback(
        domain: str, service: str, data: dict[str, object], *, blocking: bool
    ) -> None:
        if data["entity_id"] != "switch.priority":
            await FakeServices(hass.states).async_call(
                domain, service, data, blocking=blocking
            )

    hass.services.async_call = no_priority_readback
    coordinator = RoomCoordinator(
        hass,
        rooms=(room,),
        enabled=True,
        prioritize_external_sensor=True,
        max_sensor_age=timedelta(hours=2),
    )

    asyncio.run(coordinator.async_evaluate_all(now=now, reason="test"))

    assert coordinator.statuses["office"].last_error is not None
    assert "priority" in coordinator.statuses["office"].last_error
