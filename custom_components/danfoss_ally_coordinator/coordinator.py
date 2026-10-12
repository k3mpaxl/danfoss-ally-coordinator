"""Runtime coordinator for room-scoped Danfoss Ally automation."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    ADAPTATION_HEAT_DELAY,
    ADAPTATION_RETRY,
    CONF_EXCLUDED_ENTITIES,
    CONF_LOAD_BALANCING_AREAS,
    CONF_SETPOINT_SYNC_DISABLED_AREAS,
    DEFAULT_AGGREGATION,
    EXERCISE_STAGGER_MINUTES,
    HEAT_AVAILABLE_REFRESH,
    LOCAL_EVALUATION_INTERVAL,
    MAINTENANCE_RETRY,
    TIME_SYNC_INTERVAL,
)
from .discovery import RoomDefinition, ValveTarget
from .maintenance import (
    CLOCK_LOST_ERROR,
    FLOOR,
    RADIATOR,
    DemandSource,
    HouseDemand,
    exercise_plan,
    house_demand,
    time_attributes,
)
from .planner import (
    DEFAULT_CALIBRATION_LIMIT,
    LOAD_INVALID,
    LOAD_SEND_INTERVAL,
    LoadReading,
    SyncRecord,
    TemperatureReading,
    average_temperature_celsius,
    classify_reading,
    count_valid_readings,
    desired_local_calibration,
    room_load_mean,
    should_push_temperature,
)
from .store import SyncStateStore

_LOGGER = logging.getLogger(__name__)

# Let a turning dial settle before the room follows its final setpoint.
SETPOINT_SETTLE_DELAY = 3.0
SETPOINT_ECHO_WINDOW = timedelta(minutes=2)
MIN_SETPOINT = 5.0
MAX_SETPOINT = 35.0

# ZHA enum names of the valve's own window detection (0x4000), catalogue §3.3.
VALVE_WINDOW_OPEN = "Open"
VALVE_WINDOW_STATES = frozenset({"Quarantine", "Closed", "Maybe", "Open", "External"})


def observed_at(state: State) -> datetime:
    """Return when the source last confirmed this state, changed or unchanged."""
    return max(state.last_updated, state.last_reported)


def _finite(value: Any) -> float | None:
    """Parse one HA state/attribute value without trusting its type."""
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def _setpoint(state: State | None) -> float | None:
    """Return a climate entity's target temperature if it is a valid setpoint."""
    if state is None:
        return None
    target = _finite(state.attributes.get("temperature"))
    if target is None or not MIN_SETPOINT <= target <= MAX_SETPOINT:
        return None
    return target


def _clamp_to_limits(target: float, climate: State) -> float:
    """Respect a valve's own min/max setpoint limits (0x0015/0x0016).

    Sending more would be rejected by the valve, and its reported limit would
    then look like a new change to the rest of the room.
    """
    minimum = _finite(climate.attributes.get("min_temp"))
    maximum = _finite(climate.attributes.get("max_temp"))
    if maximum is not None:
        target = min(target, maximum)
    if minimum is not None:
        target = max(target, minimum)
    return target


@dataclass(slots=True)
class RoomStatus:
    """Observable computed state for one configured HA area."""

    mean_temperature: float | None = None
    valid_temperature_sources: int = 0
    window_open: bool | None = None
    unavailable_window_contacts: tuple[str, ...] = ()
    room_load_mean: int | None = None
    offline_valves: tuple[str, ...] = ()
    last_error: str | None = None
    last_reason: str | None = None
    evaluated_at: datetime | None = None


class RoomCoordinator:
    """Synchronize discovered room sources to typed, existing Danfoss ZHA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        rooms: tuple[RoomDefinition, ...],
        configuration: dict[str, Any] | None = None,
        enabled: bool,
        max_sensor_age: timedelta,
        sync_temperature: bool = True,
        sync_windows: bool = True,
        sync_valve_windows: bool = False,
        aggregation: str = DEFAULT_AGGREGATION,
        excluded_entity_ids: frozenset[str] = frozenset(),
        setpoint_sync_disabled_areas: frozenset[str] = frozenset(),
        load_balancing_areas: frozenset[str] = frozenset(),
        startup_delay: timedelta = timedelta(0),
        heat_source_entity_id: str | None = None,
        adaptation_with_heat: bool = False,
        exercise_day: str | None = None,
        exercise_time: time = time(10, 0),
        demand_climates: dict[str, str] | None = None,
        sync_records: dict[str, SyncRecord] | None = None,
        sync_state_store: SyncStateStore | None = None,
    ) -> None:
        self.hass = hass
        # The entry settings this coordinator was built from (reload detection).
        self.configuration = configuration or {}
        self.rooms = rooms
        self.enabled = enabled
        self.max_sensor_age = max_sensor_age

        self.sync_temperature = sync_temperature
        self.sync_windows = sync_windows
        self.sync_valve_windows = sync_valve_windows
        self.aggregation = aggregation
        self.excluded_entity_ids = frozenset(excluded_entity_ids)
        self.setpoint_sync_disabled_areas = frozenset(setpoint_sync_disabled_areas)
        self.load_balancing_areas = frozenset(load_balancing_areas)
        # Persists changed room-switch settings (set by the config entry setup).
        self.on_live_settings_changed: Callable[[dict[str, list[str]]], None] | None = (
            None
        )
        self._load_sent_at: dict[str, datetime] = {}
        # Catalogue §2.7: the system-wide "heat available" source and when each
        # valve last confirmed the forwarded value.
        self.heat_source_entity_id = heat_source_entity_id
        self._heat_sent_at: dict[str, datetime] = {}
        # Valve upkeep (catalogue §1.2 clock, §1.6 adaptation, §3.7 exercise).
        self.adaptation_with_heat = adaptation_with_heat
        self.exercise_day = exercise_day
        self.exercise_time = exercise_time
        self._heat_on_since: datetime | None = None
        self._maintenance_tried: dict[tuple[str, str], datetime] = {}
        self._maintenance_running: set[str] = set()
        # Extra demand sources (e.g. floor heating): climate entity -> room name.
        self.demand_climates = dict(demand_climates or {})
        self.startup_delay = startup_delay
        # Set after a Home Assistant restart: values from before it are unconfirmed.
        self._ha_started_at: datetime | None = None
        self._restarting = False
        self.writes_resume_at: datetime | None = None
        self.statuses = {room.area_id: RoomStatus() for room in rooms}
        self._sync_records = sync_records or {}
        self._sync_state_store = sync_state_store
        self._expected_setpoint_echoes: dict[str, tuple[float, datetime]] = {}
        self._valve_temperature_changed_at: dict[str, datetime] = {}
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Task] = set()
        self._evaluation_lock = asyncio.Lock()
        self._evaluation_queued = False
        self._started = False
        self._index_entities()

    def _index_entities(self) -> None:
        """Pre-compute which state changes matter for decisions or diagnostics."""
        self._tracked_source_ids = frozenset(
            entity_id
            for room in self.rooms
            for entity_id in (
                *room.temperature_entity_ids,
                *room.window_entity_ids,
                *(
                    valve.window_detection_entity_id
                    for valve in room.valves
                    if self.sync_valve_windows and valve.window_detection_entity_id
                ),
                # A mode change switches the external-temperature cadence.
                *(
                    valve.covered_entity_id
                    for valve in room.valves
                    if valve.covered_entity_id
                ),
            )
        ) | frozenset(filter(None, (self.heat_source_entity_id,)))
        self._climate_entity_ids = frozenset(
            valve.climate_entity_id
            for room in self.rooms
            for valve in room.valves
            if valve.climate_entity_id
        )

    async def async_start(self) -> None:
        """Start local event and cadence evaluation without polling Zigbee devices.

        Diagnostics are calculated immediately, but no valve is written before Home
        Assistant has started and every source integration had a chance to load.
        """
        self._unsubscribers.append(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._async_handle_state_changed
            )
        )
        self._unsubscribers.append(
            async_track_time_interval(
                self.hass, self._async_handle_interval, LOCAL_EVALUATION_INTERVAL
            )
        )
        self._restarting = not self.hass.is_running
        await self.async_evaluate_all(
            now=dt_util.utcnow(), reason="setup", allow_writes=False
        )
        self._unsubscribers.append(async_at_started(self.hass, self._async_started))

    async def _async_started(self, _hass: HomeAssistant) -> None:
        """Allow writes once Home Assistant runs and, after a restart, has settled.

        After a restart the Zigbee mesh re-forms and sleepy devices check in over
        several minutes; writing earlier risks timeouts and stale room values.
        """
        if self._restarting:
            now = dt_util.utcnow()
            self._ha_started_at = now
            if self.startup_delay > timedelta(0):
                self.writes_resume_at = now + self.startup_delay
                self._unsubscribers.append(
                    async_call_later(
                        self.hass, self.startup_delay, self._async_startup_delay_over
                    )
                )
                async with self._evaluation_lock:
                    await self.async_evaluate_all(
                        now=now, reason="startup_delay", allow_writes=False
                    )
                return
        await self._async_startup_delay_over(None)

    async def _async_startup_delay_over(self, _now: datetime | None) -> None:
        self._started = True
        self.writes_resume_at = None
        await self._async_evaluate_serial(reason="startup")

    async def async_stop(self) -> None:
        """Remove all listeners and pending work before the config entry unloads."""
        self._started = False
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        for task in tuple(self._tasks):
            task.cancel()
        self._tasks.clear()

    def _create_task(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Track background work so unloading cancels it."""
        task = self.hass.async_create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @callback
    def _async_handle_state_changed(self, event) -> None:
        """Evaluate promptly for sources, never for own write targets."""
        entity_id = event.data.get("entity_id")
        if entity_id in self._climate_entity_ids:
            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")
            self.async_observe_valve_state(entity_id, old_state, new_state)
            if self._started and self.enabled:
                self._create_task(
                    self._async_delayed_setpoint_change(entity_id, old_state, new_state)
                )
        if entity_id in self._tracked_source_ids:
            if self._started:
                self._create_task(
                    self._async_evaluate_serial(reason="source_state_changed")
                )
            elif self.writes_resume_at is not None:
                # Keep diagnostics current during the start-up delay; no writes.
                self._create_task(
                    self._async_evaluate_serial(
                        reason="startup_delay", allow_writes=False
                    )
                )

    @callback
    def async_observe_valve_state(
        self, entity_id: str, old_state: State | None, new_state: State | None
    ) -> None:
        """Remember when a valve's own temperature reading demonstrably changed.

        A climate state can be refreshed by any thermostat attribute, so only a
        changed ``current_temperature`` proves that the valve reading itself is live.
        """
        if old_state is None or new_state is None:
            return
        old_value = _finite(old_state.attributes.get("current_temperature"))
        new_value = _finite(new_state.attributes.get("current_temperature"))
        if old_value is not None and new_value is not None and old_value != new_value:
            self._valve_temperature_changed_at[entity_id] = new_state.last_updated
            self._notify_listeners()

    def fresh_valve_temperature(
        self, valve: ValveTarget, *, now: datetime
    ) -> float | None:
        """Return the valve's reported temperature only if it is demonstrably live."""
        if valve.climate_entity_id is None:
            return None
        changed_at = self._valve_temperature_changed_at.get(valve.climate_entity_id)
        climate = self.hass.states.get(valve.climate_entity_id)
        if (
            changed_at is None
            or climate is None
            or now - changed_at > self.max_sensor_age
        ):
            return None
        return _finite(climate.attributes.get("current_temperature"))

    def valve_online(self, valve: ValveTarget) -> bool:
        """Whether ZHA currently reaches the valve.

        ZHA marks every entity of an unreachable device `unavailable`. An entity
        without any state is not set up yet; its service call fails on its own.
        """
        entity_id = valve.climate_entity_id or valve.temperature_entity_id
        state = self.hass.states.get(entity_id)
        return state is None or state.state != STATE_UNAVAILABLE

    def valve_errors(self, valve: ValveTarget) -> list[str]:
        """Active bits of the valve's error code (0x0B05/0x4000), e.g. a lost clock.

        ZHA's built-in Danfoss quirk exposes each bit as a boolean attribute.
        """
        if valve.error_entity_id is None:
            return []
        state = self.hass.states.get(valve.error_entity_id)
        if state is None:
            return []
        return sorted(name for name, value in state.attributes.items() if value is True)

    def adaptation_running(self, valve: ValveTarget) -> bool:
        """Whether the valve is learning its valve characteristic right now."""
        if valve.adaptation_status_entity_id is None:
            return False
        state = self.hass.states.get(valve.adaptation_status_entity_id)
        return state is not None and state.attributes.get("In_progress") is True

    def _confirmed_since_restart(self, state: State) -> bool:
        """After a restart, only values reported since then show a live source."""
        return self._ha_started_at is None or observed_at(state) >= self._ha_started_at

    def heat_available(self) -> bool | None:
        """Whether the heating system supplies heat; None without a usable source."""
        if not self.heat_source_entity_id:
            return None
        state = self.hass.states.get(self.heat_source_entity_id)
        if state is None or state.state not in (STATE_ON, STATE_OFF):
            return None
        return state.state == STATE_ON

    def valve_covered(self, valve: ValveTarget) -> bool | None:
        """Read the valve's covered-radiator mode; this integration never writes it."""
        if valve.covered_entity_id is None:
            return None
        state = self.hass.states.get(valve.covered_entity_id)
        if state is None or state.state not in (STATE_ON, STATE_OFF):
            return None
        return state.state == STATE_ON

    async def _async_delayed_setpoint_change(
        self, entity_id: str, old_state: State | None, new_state: State | None
    ) -> None:
        """Act only on the setpoint a valve settled on, not on each dial step."""
        await asyncio.sleep(SETPOINT_SETTLE_DELAY)
        current = _setpoint(self.hass.states.get(entity_id))
        if current is None or current != _setpoint(new_state):
            return
        await self.async_handle_setpoint_change(entity_id, old_state, new_state)

    def _find_valve(
        self, climate_entity_id: str
    ) -> tuple[RoomDefinition, ValveTarget] | tuple[None, None]:
        for room in self.rooms:
            for valve in room.valves:
                if valve.climate_entity_id == climate_entity_id:
                    return room, valve
        return None, None

    def _consume_setpoint_echo(self, entity_id: str, target: float) -> bool:
        """Return True once for the state change caused by our own mirrored write."""
        expected = self._expected_setpoint_echoes.get(entity_id)
        if expected is None:
            return False
        value, expires_at = expected
        if dt_util.utcnow() > expires_at:
            del self._expected_setpoint_echoes[entity_id]
            return False
        if abs(value - target) < 0.05:
            del self._expected_setpoint_echoes[entity_id]
            return True
        return False

    def setpoint_sync_enabled(self, room: RoomDefinition) -> bool:
        """Whether a room keeps its valves' target temperatures equal (default on)."""
        return room.area_id not in self.setpoint_sync_disabled_areas

    async def async_handle_setpoint_change(
        self, entity_id: str, old_state: State | None, new_state: State | None
    ) -> None:
        """Give every valve in the room the target another valve just changed to.

        Changes at the dial, from HA users, automations or a valve schedule are all
        followed. This integration's own writes are recognised as echoes and never
        trigger another round.
        """
        if not self.enabled:
            return
        target = _setpoint(new_state)
        previous = _setpoint(old_state)
        if target is None or previous is None or target == previous:
            return
        if self._consume_setpoint_echo(entity_id, target):
            return
        room, source = self._find_valve(entity_id)
        if room is None or source is None or len(room.valves) < 2:
            return
        if not self.setpoint_sync_enabled(room):
            return
        async with self._evaluation_lock:
            status = self.statuses[room.area_id]
            for valve in room.valves:
                if valve is source or valve.climate_entity_id is None:
                    continue
                peer = self.hass.states.get(valve.climate_entity_id)
                if peer is None or not self.valve_online(valve):
                    continue
                if self.adaptation_running(valve):
                    # Catalogue §1.6: a user-interaction setpoint cancels the run.
                    continue
                peer_value = _clamp_to_limits(target, peer)
                peer_target = _setpoint(peer)
                if peer_target is not None and abs(peer_target - peer_value) < 0.05:
                    continue
                self._expected_setpoint_echoes[valve.climate_entity_id] = (
                    peer_value,
                    dt_util.utcnow() + SETPOINT_ECHO_WINDOW,
                )
                try:
                    await self.hass.services.async_call(
                        "climate",
                        "set_temperature",
                        {
                            ATTR_ENTITY_ID: valve.climate_entity_id,
                            "temperature": peer_value,
                        },
                        blocking=True,
                    )
                except HomeAssistantError as err:
                    self._expected_setpoint_echoes.pop(valve.climate_entity_id, None)
                    status.last_error = f"{valve.climate_entity_id}: {err}"
                    _LOGGER.warning("Danfoss setpoint mirroring failed: %s", err)
                    continue
                readback = _setpoint(self.hass.states.get(valve.climate_entity_id))
                if readback is None or abs(readback - peer_value) >= 0.05:
                    # Keep the echo expectation: the valve may still apply it late.
                    status.last_error = (
                        f"{valve.climate_entity_id}: setpoint readback mismatch "
                        f"(expected {peer_value}, got {readback})"
                    )
                    _LOGGER.warning(
                        "Danfoss setpoint readback mismatch: %s", status.last_error
                    )
            status.last_reason = "setpoint_sync"
            self._notify_listeners()

    async def async_calibrate_local_sensor(
        self, climate_entity_id: str, *, now: datetime
    ) -> dict[str, Any]:
        """One-shot, operator-triggered local temperature calibration for one valve."""
        async with self._evaluation_lock:
            room, valve = self._find_valve(climate_entity_id)
            if room is None or valve is None:
                raise HomeAssistantError("Valve is not in a selected Danfoss room")
            if valve.calibration_entity_id is None:
                raise HomeAssistantError("No local calibration number for this valve")
            if not self.valve_online(valve):
                raise HomeAssistantError("The valve is currently not reachable")
            if self.valve_covered(valve):
                raise HomeAssistantError(
                    "The valve is in covered-radiator mode and regulates on the "
                    "external room temperature; a local offset has no effect"
                )
            mean = average_temperature_celsius(
                self._temperature_readings(room),
                now=now,
                max_age=self.max_sensor_age,
                method=self.aggregation,
            )
            if mean is None:
                raise HomeAssistantError("No fresh room temperature is available")
            valve_temperature = self.fresh_valve_temperature(valve, now=now)
            if valve_temperature is None:
                raise HomeAssistantError(
                    "The valve has not reported a changed temperature recently; "
                    "try again after it reports a new reading"
                )
            offset_state = self.hass.states.get(valve.calibration_entity_id)
            current_offset = _finite(offset_state.state) if offset_state else None
            if offset_state is None or current_offset is None:
                raise HomeAssistantError("The current local offset is unavailable")
            minimum = _finite(offset_state.attributes.get("min"))
            maximum = _finite(offset_state.attributes.get("max"))
            desired = desired_local_calibration(
                room_temperature=mean,
                valve_temperature=valve_temperature,
                current_offset=current_offset,
                minimum=-DEFAULT_CALIBRATION_LIMIT if minimum is None else minimum,
                maximum=DEFAULT_CALIBRATION_LIMIT if maximum is None else maximum,
            )
            result = {
                "entity_id": valve.calibration_entity_id,
                "room_temperature": mean,
                "valve_temperature": valve_temperature,
                "previous_offset": current_offset,
                "offset": desired,
            }
            if abs(desired - current_offset) < 0.05:
                return result
            await self.hass.services.async_call(
                "number",
                "set_value",
                {ATTR_ENTITY_ID: valve.calibration_entity_id, "value": desired},
                blocking=True,
            )
            confirmed = self.hass.states.get(valve.calibration_entity_id)
            actual = _finite(confirmed.state) if confirmed else None
            if actual is None or abs(actual - desired) > 0.05:
                raise HomeAssistantError("Local calibration readback mismatch")
            self.statuses[room.area_id].last_reason = "manual_calibration"
            self._notify_listeners()
            return result

    async def _async_handle_interval(self, _now: datetime) -> None:
        """Keep the Danfoss external-sensor lease alive before it expires."""
        if self._started:
            await self._async_evaluate_serial(reason="cadence")

    async def _async_evaluate_serial(
        self, *, reason: str, allow_writes: bool = True
    ) -> None:
        """Serialize service calls so concurrent HA state events cannot burst radios.

        While a slow valve write holds the lock, at most one evaluation waits; it
        reads the current state when it runs, so further requests add nothing.
        """
        if self._evaluation_queued:
            return
        self._evaluation_queued = True
        try:
            await self._evaluation_lock.acquire()
        except BaseException:
            self._evaluation_queued = False
            raise
        try:
            self._evaluation_queued = False
            await self.async_evaluate_all(
                now=dt_util.utcnow(), reason=reason, allow_writes=allow_writes
            )
        finally:
            self._evaluation_lock.release()

    async def async_refresh(
        self, discover: Callable[[], Coroutine[Any, Any, tuple[RoomDefinition, ...]]]
    ) -> None:
        """Adopt fresh registry discovery and evaluate it without racing writes."""
        async with self._evaluation_lock:
            rooms = await discover()
            if rooms:
                self.async_replace_rooms(rooms)
            else:
                _LOGGER.warning(
                    "Skipping empty Danfoss rediscovery; preserving the previous set"
                )
            await self.async_evaluate_all(
                now=dt_util.utcnow(),
                reason="manual_refresh",
                allow_writes=self._started,
            )

    def known_entity_ids(self) -> frozenset[str]:
        """Every source and valve entity the current room discovery relies on."""
        return (
            frozenset(
                entity_id
                for room in self.rooms
                for entity_id in (
                    *room.temperature_entity_ids,
                    *room.window_entity_ids,
                    *(
                        value
                        for valve in room.valves
                        for value in (
                            valve.temperature_entity_id,
                            valve.window_entity_id,
                            valve.covered_entity_id,
                            valve.climate_entity_id,
                            valve.window_detection_entity_id,
                            valve.calibration_entity_id,
                            valve.load_estimate_entity_id,
                            valve.load_room_mean_entity_id,
                            valve.load_balancing_enable_entity_id,
                            valve.adaptation_status_entity_id,
                            valve.error_entity_id,
                            valve.heat_available_entity_id,
                            valve.heat_required_entity_id,
                            valve.pi_demand_entity_id,
                            valve.adaptation_control_entity_id,
                            valve.adaptation_settings_entity_id,
                            valve.exercise_day_entity_id,
                            valve.exercise_time_entity_id,
                        )
                        if value
                    ),
                )
            )
            | self.excluded_entity_ids
            | frozenset(filter(None, (self.heat_source_entity_id,)))
            | frozenset(self.demand_climates)
        )

    def live_settings(self) -> dict[str, list[str]]:
        """The room-switch settings, in the form stored in the entry options."""
        return {
            CONF_EXCLUDED_ENTITIES: sorted(self.excluded_entity_ids),
            CONF_SETPOINT_SYNC_DISABLED_AREAS: sorted(
                self.setpoint_sync_disabled_areas
            ),
            CONF_LOAD_BALANCING_AREAS: sorted(self.load_balancing_areas),
        }

    @staticmethod
    def _toggled(values: frozenset[str], item: str, member: bool) -> frozenset[str]:
        return values | {item} if member else values - {item}

    async def async_set_source_enabled(self, entity_id: str, enabled: bool) -> None:
        """Include or ignore one discovered source and re-evaluate its room."""
        await self._async_change_live_settings(
            excluded=self._toggled(self.excluded_entity_ids, entity_id, not enabled)
        )

    async def async_set_setpoint_sync(self, area_id: str, enabled: bool) -> None:
        """Switch target-temperature synchronisation for one room on or off."""
        await self._async_change_live_settings(
            setpoint_sync_disabled=self._toggled(
                self.setpoint_sync_disabled_areas, area_id, not enabled
            )
        )

    async def async_set_load_balancing(self, area_id: str, enabled: bool) -> None:
        """Switch catalogue §2.2 load balancing for one room on or off."""
        if enabled and not any(
            room.area_id == area_id and self.load_balancing_supported(room)
            for room in self.rooms
        ):
            return
        await self._async_change_live_settings(
            load_balancing=self._toggled(self.load_balancing_areas, area_id, enabled)
        )

    async def _async_change_live_settings(self, **changes: frozenset[str]) -> None:
        """Apply a room switch at once; radio writes follow in the background.

        Writes to battery valves can take minutes, so the switch must not wait.
        """
        released = self._set_live_settings(**changes)
        if released is None:
            return
        self._notify_listeners()
        if self.on_live_settings_changed:
            self.on_live_settings_changed(self.live_settings())
        self._create_task(self._async_apply_settings_effects(released))

    async def async_apply_live_settings(
        self,
        *,
        excluded: frozenset[str] | None = None,
        setpoint_sync_disabled: frozenset[str] | None = None,
        load_balancing: frozenset[str] | None = None,
    ) -> None:
        """Adopt room-switch settings from the options without reloading."""
        released = self._set_live_settings(
            excluded=excluded,
            setpoint_sync_disabled=setpoint_sync_disabled,
            load_balancing=load_balancing,
        )
        if released is None:
            return
        self._notify_listeners()
        await self._async_apply_settings_effects(released)

    def _set_live_settings(
        self,
        *,
        excluded: frozenset[str] | None = None,
        setpoint_sync_disabled: frozenset[str] | None = None,
        load_balancing: frozenset[str] | None = None,
    ) -> frozenset[str] | None:
        """Store new settings; return rooms whose load balancing ended, or None."""
        excluded = self.excluded_entity_ids if excluded is None else excluded
        if setpoint_sync_disabled is None:
            setpoint_sync_disabled = self.setpoint_sync_disabled_areas
        if load_balancing is None:
            load_balancing = self.load_balancing_areas
        if (excluded, setpoint_sync_disabled, load_balancing) == (
            self.excluded_entity_ids,
            self.setpoint_sync_disabled_areas,
            self.load_balancing_areas,
        ):
            return None
        released = self.load_balancing_areas - load_balancing
        self.excluded_entity_ids = frozenset(excluded)
        self.setpoint_sync_disabled_areas = frozenset(setpoint_sync_disabled)
        self.load_balancing_areas = frozenset(load_balancing)
        for area_id in released:
            if area_id in self.statuses:
                self.statuses[area_id].room_load_mean = None
        return released

    async def _async_apply_settings_effects(self, released: frozenset[str]) -> None:
        """Release ended load balancing and re-evaluate with the new settings."""
        async with self._evaluation_lock:
            if self._started and self.enabled:
                for room in self.rooms:
                    if room.area_id in released:
                        await self._async_release_load(room)
            await self.async_evaluate_all(
                now=dt_util.utcnow(),
                reason="settings_changed",
                allow_writes=self._started,
            )

    def source_details(self, room: RoomDefinition) -> dict[str, list[dict[str, Any]]]:
        """Explain, per discovered source, whether and why it is used.

        Freshness is judged at the room's last evaluation, matching its mean.
        """
        now = self.statuses[room.area_id].evaluated_at or dt_util.utcnow()
        temperatures: list[dict[str, Any]] = []
        for entity_id in room.temperature_entity_ids:
            state = self.hass.states.get(entity_id)
            value = _finite(state.state) if state is not None else None
            if entity_id in self.excluded_entity_ids:
                status = "ignored"
            elif state is None:
                status = "unavailable"
            elif not self._confirmed_since_restart(state):
                status = "waiting_since_restart"
            else:
                status, _ = classify_reading(
                    TemperatureReading(
                        entity_id,
                        value,
                        state.attributes.get("unit_of_measurement"),
                        observed_at(state),
                    ),
                    now=now,
                    max_age=self.max_sensor_age,
                )
            temperatures.append(
                {"entity_id": entity_id, "value": value, "status": status}
            )
        contacts: list[dict[str, Any]] = []
        for entity_id in room.window_entity_ids:
            state = self.hass.states.get(entity_id)
            if entity_id in self.excluded_entity_ids:
                status = "ignored"
            elif state is None or state.state not in (STATE_ON, STATE_OFF):
                status = "unavailable"
            else:
                status = "open" if state.state == STATE_ON else "closed"
            contacts.append({"entity_id": entity_id, "status": status})
        return {"temperature": temperatures, "window": contacts}

    @callback
    def async_replace_rooms(self, rooms: tuple[RoomDefinition, ...]) -> None:
        """Adopt fresh discovery while preserving unchanged room diagnostics."""
        previous_statuses = self.statuses
        self.rooms = rooms
        self.statuses = {
            room.area_id: previous_statuses.get(room.area_id, RoomStatus())
            for room in rooms
        }
        self._index_entities()
        self._notify_listeners()

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe an entity to status updates."""
        self._listeners.append(listener)

        def remove_listener() -> None:
            self._listeners.remove(listener)

        return remove_listener

    def _notify_listeners(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def _temperature_readings(
        self, room: RoomDefinition
    ) -> tuple[TemperatureReading, ...]:
        """Turn HA states into planner inputs without trusting invalid state strings.

        Freshness uses the last report, so a steady sensor stays valid as long as
        its integration keeps confirming the same value.
        """
        readings: list[TemperatureReading] = []
        for entity_id in room.temperature_entity_ids:
            if entity_id in self.excluded_entity_ids:
                continue
            if (state := self.hass.states.get(entity_id)) is None:
                continue
            if not self._confirmed_since_restart(state):
                continue
            readings.append(
                TemperatureReading(
                    entity_id=entity_id,
                    value=_finite(state.state),
                    unit=state.attributes.get("unit_of_measurement"),
                    observed_at=observed_at(state),
                )
            )
        return tuple(readings)

    def _room_window_open(
        self, room: RoomDefinition
    ) -> tuple[bool | None, tuple[str, ...]]:
        """Aggregate the room's opening state and list unknown contacts.

        Any available open contact opens the room. Unavailable contacts neither open
        nor close it, so one dead contact cannot block the room forever; if no
        contact is available, the state is unknown and no `closed` is written.
        """
        unavailable: list[str] = []
        available = False
        window_open: bool | None = False
        contacts = [
            entity_id
            for entity_id in room.window_entity_ids
            if entity_id not in self.excluded_entity_ids
        ]
        for entity_id in contacts:
            state = self.hass.states.get(entity_id)
            if state is None or state.state not in (STATE_ON, STATE_OFF):
                unavailable.append(entity_id)
                continue
            available = True
            if state.state == STATE_ON:
                window_open = True
        if not available:
            window_open = None

        if self.sync_valve_windows:
            reports = [
                self.hass.states.get(valve.window_detection_entity_id)
                for valve in room.valves
                if valve.window_detection_entity_id
            ]
            known = [
                state.state
                for state in reports
                if state is not None and state.state in VALVE_WINDOW_STATES
            ]
            if VALVE_WINDOW_OPEN in known:
                # Only a local detection propagates; `External` is our own signal.
                window_open = True
            elif not contacts and reports and len(known) == len(reports):
                # Catalogue §3.3: release once no valve reports a local opening.
                window_open = False
        return window_open, tuple(unavailable)

    async def async_evaluate_all(
        self, *, now: datetime, reason: str, allow_writes: bool = True
    ) -> None:
        """Evaluate every room and durably save any successful temperature write."""
        if self._started and self.enabled and allow_writes:
            unsupported = {
                room.area_id: room
                for room in self.rooms
                if not self.load_balancing_supported(room)
                and (
                    room.area_id in self.load_balancing_areas
                    or self._room_has_enabled_load_balancing(room)
                )
            }
            if unsupported:
                released = self._set_live_settings(
                    load_balancing=self.load_balancing_areas - unsupported.keys()
                )
                if released is not None and self.on_live_settings_changed:
                    self.on_live_settings_changed(self.live_settings())
                for room in unsupported.values():
                    await self._async_release_load(room)
        heat = self.heat_available()
        if heat:
            self._heat_on_since = self._heat_on_since or now
        else:
            self._heat_on_since = None
        records_before = self._sync_records.copy()
        for room in tuple(self.rooms):
            await self.async_evaluate_room(
                room=room, now=now, reason=reason, allow_writes=allow_writes
            )
        if self._sync_state_store is not None and records_before != self._sync_records:
            await self._sync_state_store.async_save(self._sync_records)

    async def async_evaluate_room(
        self,
        *,
        room: RoomDefinition,
        now: datetime,
        reason: str,
        allow_writes: bool = True,
    ) -> None:
        """Calculate one room's state and issue only required typed service calls."""
        status = self.statuses[room.area_id]
        readings = self._temperature_readings(room)
        mean = average_temperature_celsius(
            readings, now=now, max_age=self.max_sensor_age, method=self.aggregation
        )
        window_open, unavailable_contacts = self._room_window_open(room)
        status.mean_temperature = mean
        status.valid_temperature_sources = count_valid_readings(
            readings, now=now, max_age=self.max_sensor_age
        )
        status.window_open = window_open
        status.unavailable_window_contacts = unavailable_contacts
        status.offline_valves = tuple(
            valve.climate_entity_id or valve.temperature_entity_id
            for valve in room.valves
            if not self.valve_online(valve)
        )
        status.last_error = None
        status.last_reason = reason
        status.evaluated_at = now

        if not self.enabled or not allow_writes:
            self._notify_listeners()
            return

        heat = self.heat_available()
        for valve in room.valves:
            if not self.valve_online(valve):
                # Not reachable (e.g. mesh still re-forming): retry next round.
                continue
            if self.sync_temperature and mean is not None:
                await self._async_push_temperature(status, valve, mean, now)
            if self.sync_windows and window_open is not None:
                await self._async_push_window(status, valve, window_open)
            if heat is not None and valve.heat_available_entity_id:
                await self._async_push_heat_available(status, valve, heat, now)
            if self._started:
                self._schedule_maintenance(valve, now)

        if self.load_balancing_active(room):
            await self._async_balance_load(room, status, now)

        self._notify_listeners()

    def load_balancing_valves(self, room: RoomDefinition) -> list[ValveTarget]:
        """Valves whose ZHA entities support room load balancing."""
        return [valve for valve in room.valves if valve.supports_load_balancing]

    def load_balancing_supported(self, room: RoomDefinition) -> bool:
        """Catalogue §2.2: only meaningful with two or more valves in a room."""
        return len(self.load_balancing_valves(room)) >= 2

    def load_balancing_active(self, room: RoomDefinition) -> bool:
        return room.area_id in self.load_balancing_areas and (
            self.load_balancing_supported(room)
        )

    def _room_has_enabled_load_balancing(self, room: RoomDefinition) -> bool:
        """Detect a device mode still enabled without relying on stored options."""
        for valve in self.load_balancing_valves(room):
            switch = self.hass.states.get(valve.load_balancing_enable_entity_id)
            number = self.hass.states.get(valve.load_room_mean_entity_id)
            if switch is not None and switch.state == STATE_ON:
                return True
            if number is not None and (value := _finite(number.state)) is not None:
                if value != LOAD_INVALID:
                    return True
        return False

    def _load_readings(self, room: RoomDefinition) -> tuple[LoadReading, ...]:
        """Each valve's load estimate with the time its valve was last heard from.

        ZHA only emits changes, so a steady estimate stays current while the same
        valve keeps reporting other thermostat values.
        """
        readings: list[LoadReading] = []
        for valve in self.load_balancing_valves(room):
            state = self.hass.states.get(valve.load_estimate_entity_id)
            if state is None:
                continue
            heard = [observed_at(state)]
            if valve.climate_entity_id and (
                climate := self.hass.states.get(valve.climate_entity_id)
            ):
                heard.append(observed_at(climate))
            readings.append(
                LoadReading(
                    valve.load_estimate_entity_id, _finite(state.state), max(heard)
                )
            )
        return tuple(readings)

    async def _async_balance_load(
        self, room: RoomDefinition, status: RoomStatus, now: datetime
    ) -> None:
        """Send the room's mean radiator load to every valve every 15 minutes."""
        mean = room_load_mean(self._load_readings(room), now=now)
        status.room_load_mean = mean
        if mean is None:
            # Without a valid estimate the valves return to normal after 90 min.
            return
        for valve in self.load_balancing_valves(room):
            if not self.valve_online(valve):
                continue
            sent_at = self._load_sent_at.get(valve.load_room_mean_entity_id)
            if sent_at is not None and now - sent_at < LOAD_SEND_INTERVAL:
                continue
            if not await self._async_ensure_switch_on(
                status, valve.load_balancing_enable_entity_id
            ):
                continue
            if await self._async_set_number(
                status, valve.load_room_mean_entity_id, mean, tolerance=0.5
            ):
                self._load_sent_at[valve.load_room_mean_entity_id] = now

    async def _async_release_load(self, room: RoomDefinition) -> None:
        """Clear the room mean and turn off the per-valve load-balancing mode."""
        status = self.statuses[room.area_id]
        for valve in self.load_balancing_valves(room):
            self._load_sent_at.pop(valve.load_room_mean_entity_id, None)
            if not self.valve_online(valve):
                continue
            state = self.hass.states.get(valve.load_room_mean_entity_id)
            if state is not None and (
                (value := _finite(state.state)) is not None and value != LOAD_INVALID
            ):
                await self._async_set_number(
                    status, valve.load_room_mean_entity_id, LOAD_INVALID, tolerance=0.5
                )
            switch = self.hass.states.get(valve.load_balancing_enable_entity_id)
            if switch is not None and switch.state == STATE_OFF:
                continue
            try:
                await self.hass.services.async_call(
                    "switch",
                    SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: valve.load_balancing_enable_entity_id},
                    blocking=True,
                )
            except HomeAssistantError as err:
                status.last_error = f"{valve.load_balancing_enable_entity_id}: {err}"
                _LOGGER.warning("Danfoss load disable failed: %s", err)
                continue
            readback = self.hass.states.get(valve.load_balancing_enable_entity_id)
            if readback is None or readback.state != STATE_OFF:
                status.last_error = (
                    f"{valve.load_balancing_enable_entity_id}: readback mismatch "
                    "(expected off)"
                )
                _LOGGER.warning(
                    "Danfoss load disable readback mismatch: %s", status.last_error
                )

    async def _async_ensure_switch_on(self, status: RoomStatus, entity_id: str) -> bool:
        """Turn a valve setting on if needed and confirm it."""
        state = self.hass.states.get(entity_id)
        if state is not None and state.state == STATE_ON:
            return True
        try:
            await self.hass.services.async_call(
                "switch", SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
            )
        except HomeAssistantError as err:
            status.last_error = f"{entity_id}: {err}"
            _LOGGER.warning("Danfoss switch write failed: %s", err)
            return False
        readback = self.hass.states.get(entity_id)
        if readback is None or readback.state != STATE_ON:
            status.last_error = f"{entity_id}: readback mismatch (expected on)"
            _LOGGER.warning("Danfoss switch readback mismatch: %s", status.last_error)
            return False
        return True

    async def _async_set_number(
        self, status: RoomStatus, entity_id: str, value: float, *, tolerance: float
    ) -> bool:
        """Write one number entity and confirm the reported value."""
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {ATTR_ENTITY_ID: entity_id, "value": value},
                blocking=True,
            )
        except HomeAssistantError as err:
            status.last_error = f"{entity_id}: {err}"
            _LOGGER.warning("Danfoss number write failed: %s", err)
            return False
        state = self.hass.states.get(entity_id)
        readback = _finite(state.state) if state is not None else None
        if readback is None or abs(readback - value) > tolerance:
            status.last_error = (
                f"{entity_id}: readback mismatch (expected {value}, got {readback})"
            )
            _LOGGER.warning("Danfoss number readback mismatch: %s", status.last_error)
            return False
        return True

    def load_diagnostics(self, room: RoomDefinition) -> dict[str, Any]:
        """Each valve's load estimate and the last mean sent to it."""
        return {
            "room_load_mean": self.statuses[room.area_id].room_load_mean,
            "valves": [
                {
                    "load_estimate": _finite(state.state)
                    if (state := self.hass.states.get(valve.load_estimate_entity_id))
                    else None,
                    "entity_id": valve.load_estimate_entity_id,
                    "last_sent_at": sent.isoformat()
                    if (sent := self._load_sent_at.get(valve.load_room_mean_entity_id))
                    else None,
                }
                for valve in self.load_balancing_valves(room)
            ],
        }

    async def _async_push_temperature(
        self, status: RoomStatus, valve: ValveTarget, mean: float, now: datetime
    ) -> None:
        """Write the room mean to one valve when the catalogue cadence requires it."""
        sent_value = round(mean, 1)
        if not should_push_temperature(
            value=sent_value,
            last_sync=self._sync_records.get(valve.temperature_entity_id),
            now=now,
            covered=bool(self.valve_covered(valve)),
        ):
            return
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {ATTR_ENTITY_ID: valve.temperature_entity_id, "value": sent_value},
                blocking=True,
            )
        except HomeAssistantError as err:
            status.last_error = f"{valve.temperature_entity_id}: {err}"
            _LOGGER.warning("Danfoss temperature sync failed: %s", err)
            return
        state = self.hass.states.get(valve.temperature_entity_id)
        readback = _finite(state.state) if state is not None else None
        if readback is None or abs(readback - sent_value) > 0.05:
            status.last_error = (
                f"{valve.temperature_entity_id}: readback mismatch "
                f"(expected {sent_value}, got {readback})"
            )
            _LOGGER.warning(
                "Danfoss temperature readback mismatch: %s", status.last_error
            )
            return
        self._sync_records[valve.temperature_entity_id] = SyncRecord(
            value=sent_value, succeeded_at=now
        )

    async def _async_push_window(
        self, status: RoomStatus, valve: ValveTarget, window_open: bool
    ) -> None:
        """Set one valve's external window signal to the room aggregate."""
        desired_state = STATE_ON if window_open else STATE_OFF
        current_state = self.hass.states.get(valve.window_entity_id)
        if current_state is not None and current_state.state == desired_state:
            return
        try:
            await self.hass.services.async_call(
                "switch",
                SERVICE_TURN_ON if window_open else SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: valve.window_entity_id},
                blocking=True,
            )
        except HomeAssistantError as err:
            status.last_error = f"{valve.window_entity_id}: {err}"
            _LOGGER.warning("Danfoss window sync failed: %s", err)
            return
        readback = self.hass.states.get(valve.window_entity_id)
        if readback is None or readback.state != desired_state:
            status.last_error = (
                f"{valve.window_entity_id}: window readback mismatch "
                f"(expected {desired_state}, got "
                f"{readback.state if readback else None})"
            )
            _LOGGER.warning("Danfoss window readback mismatch: %s", status.last_error)

    async def _async_push_heat_available(
        self, status: RoomStatus, valve: ValveTarget, available: bool, now: datetime
    ) -> None:
        """Forward the system's heat availability to one valve (catalogue §2.7).

        Written on every change and re-sent after HEAT_AVAILABLE_REFRESH, so a
        valve that missed a write or rejoined the network learns it again.
        """
        entity_id = valve.heat_available_entity_id
        assert entity_id is not None
        desired_state = STATE_ON if available else STATE_OFF
        current_state = self.hass.states.get(entity_id)
        sent_at = self._heat_sent_at.get(entity_id)
        if (
            current_state is not None
            and current_state.state == desired_state
            and sent_at is not None
            and now - sent_at < HEAT_AVAILABLE_REFRESH
        ):
            return
        try:
            await self.hass.services.async_call(
                "switch",
                SERVICE_TURN_ON if available else SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        except HomeAssistantError as err:
            status.last_error = f"{entity_id}: {err}"
            _LOGGER.warning("Danfoss heat-available write failed: %s", err)
            return
        readback = self.hass.states.get(entity_id)
        if readback is None or readback.state != desired_state:
            status.last_error = (
                f"{entity_id}: heat-available readback mismatch "
                f"(expected {desired_state}, "
                f"got {readback.state if readback else None})"
            )
            _LOGGER.warning("Danfoss heat-available mismatch: %s", status.last_error)
            return
        self._heat_sent_at[entity_id] = now

    # -- House-wide heat demand (signal only) -------------------------------------

    def demand_entity_ids(self) -> frozenset[str]:
        """Every entity the house demand is calculated from."""
        return frozenset(
            entity_id
            for room in self.rooms
            for valve in room.valves
            for entity_id in (valve.heat_required_entity_id, valve.pi_demand_entity_id)
            if entity_id
        ) | frozenset(self.demand_climates)

    def _demand_sources(self) -> list[DemandSource]:
        sources: list[DemandSource] = []
        for room in self.rooms:
            for valve in room.valves:
                required = percent = None
                if valve.heat_required_entity_id and (
                    state := self.hass.states.get(valve.heat_required_entity_id)
                ):
                    if state.state in (STATE_ON, STATE_OFF):
                        required = state.state == STATE_ON
                if valve.pi_demand_entity_id and (
                    state := self.hass.states.get(valve.pi_demand_entity_id)
                ):
                    percent = _finite(state.state)
                if required is not None or percent is not None:
                    sources.append(DemandSource(room.name, RADIATOR, required, percent))
        for entity_id, room_name in self.demand_climates.items():
            state = self.hass.states.get(entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                continue
            heating = state.attributes.get("hvac_action") == "heating"
            sources.append(DemandSource(room_name, FLOOR, heating))
        return sources

    def house_demand(self) -> HouseDemand:
        """Radiator and floor-heating demand, apart and combined."""
        return house_demand(self._demand_sources())

    # -- Valve upkeep -------------------------------------------------------------

    def exercise_targets(self) -> dict[str, tuple[str, int]]:
        """The exercise day and minute of day each valve should have (§3.7)."""
        if self.exercise_day is None:
            return {}
        return exercise_plan(
            (
                valve.device_id
                for room in self.rooms
                for valve in room.valves
                if valve.exercise_day_entity_id and valve.exercise_time_entity_id
            ),
            self.exercise_day,
            self.exercise_time,
            EXERCISE_STAGGER_MINUTES,
        )

    def _may_try(self, kind: str, valve: ValveTarget, now: datetime, wait) -> bool:
        tried = self._maintenance_tried.get((kind, valve.device_id))
        return tried is None or now - tried >= wait

    def time_sync_due(self, valve: ValveTarget, now: datetime) -> bool:
        """Catalogue §1.2: weekly, and at once when the valve reports a lost clock."""
        if not valve.ieee or not self._may_try("time", valve, now, MAINTENANCE_RETRY):
            return False
        if CLOCK_LOST_ERROR in self.valve_errors(valve):
            return True
        record = self._sync_records.get(f"time:{valve.device_id}")
        return record is None or now - record.succeeded_at >= TIME_SYNC_INTERVAL

    def exercise_due(self, valve: ValveTarget, now: datetime) -> tuple[str, int] | None:
        """The (day, minute) to write when the exercise differs from the plan."""
        target = self.exercise_targets().get(valve.device_id)
        if target is None or not self._may_try(
            "exercise", valve, now, MAINTENANCE_RETRY
        ):
            return None
        day = self.hass.states.get(valve.exercise_day_entity_id)
        minute = self.hass.states.get(valve.exercise_time_entity_id)
        if day is None or minute is None:
            return None
        if day.state == target[0] and _finite(minute.state) == target[1]:
            return None
        return target

    def _adaptation_status(self, valve: ValveTarget) -> dict[str, Any] | None:
        if valve.adaptation_status_entity_id is None:
            return None
        state = self.hass.states.get(valve.adaptation_status_entity_id)
        return None if state is None else dict(state.attributes)

    def adaptation_start_due(self, valve: ValveTarget, now: datetime) -> bool:
        """Catalogue §1.6: start the run once heat has been available for a while.

        Only for a valve that has not found its characteristic and is not running
        an adaptation already, and at most once a day.
        """
        if not (self.adaptation_with_heat and valve.adaptation_control_entity_id):
            return False
        if (
            self._heat_on_since is None
            or now - self._heat_on_since < ADAPTATION_HEAT_DELAY
        ):
            return False
        status = self._adaptation_status(valve)
        if status is None or status.get("Valve_characteristic_found") is True:
            return False
        if status.get("In_progress") is True:
            return False
        return self._may_try("adaptation", valve, now, ADAPTATION_RETRY)

    def nightly_adaptation_off_due(self, valve: ValveTarget, now: datetime) -> bool:
        """With heat-led adaptation the valve's own nightly run is switched off."""
        if not (self.adaptation_with_heat and self.heat_source_entity_id):
            return False
        if valve.adaptation_settings_entity_id is None:
            return False
        state = self.hass.states.get(valve.adaptation_settings_entity_id)
        return (
            state is not None
            and state.state == STATE_ON
            and self._may_try("adaptation_settings", valve, now, MAINTENANCE_RETRY)
        )

    @callback
    def _schedule_maintenance(self, valve: ValveTarget, now: datetime) -> None:
        """Queue due upkeep for one valve; battery writes run in the background."""
        if valve.device_id in self._maintenance_running:
            return
        tasks: dict[str, Any] = {}
        if self.time_sync_due(valve, now):
            tasks["time"] = True
        if exercise := self.exercise_due(valve, now):
            tasks["exercise"] = exercise
        if self.nightly_adaptation_off_due(valve, now):
            tasks["adaptation_settings"] = True
        if self.adaptation_start_due(valve, now):
            tasks["adaptation"] = True
        if not tasks:
            return
        for kind in tasks:
            self._maintenance_tried[(kind, valve.device_id)] = now
        self._maintenance_running.add(valve.device_id)
        self._create_task(self._async_maintain_valve(valve, tasks, now))

    async def _async_call(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> bool:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Danfoss upkeep %s.%s failed: %s", domain, service, err)
            return False
        return True

    async def _async_maintain_valve(
        self, valve: ValveTarget, tasks: dict[str, Any], now: datetime
    ) -> None:
        """Run one valve's due upkeep in order; each step is independent."""
        try:
            if tasks.get("time") and await self._async_write_time(valve, now):
                self._sync_records[f"time:{valve.device_id}"] = SyncRecord(0.0, now)
                if self._sync_state_store is not None:
                    await self._sync_state_store.async_save(self._sync_records)
            if exercise := tasks.get("exercise"):
                day, minute = exercise
                await self._async_call(
                    "select",
                    "select_option",
                    {ATTR_ENTITY_ID: valve.exercise_day_entity_id, "option": day},
                )
                await self._async_call(
                    "number",
                    "set_value",
                    {ATTR_ENTITY_ID: valve.exercise_time_entity_id, "value": minute},
                )
            if tasks.get("adaptation_settings"):
                await self._async_call(
                    "switch",
                    SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: valve.adaptation_settings_entity_id},
                )
            if tasks.get("adaptation"):
                await self._async_call(
                    "select",
                    "select_option",
                    {
                        ATTR_ENTITY_ID: valve.adaptation_control_entity_id,
                        "option": "Initiate",
                    },
                )
        finally:
            self._maintenance_running.discard(valve.device_id)
            self._notify_listeners()

    async def _async_write_time(self, valve: ValveTarget, now: datetime) -> bool:
        """Write the six Time cluster attributes of catalogue §1.2."""
        tz = ZoneInfo(str(self.hass.config.time_zone))
        for attribute, value in time_attributes(now, tz).items():
            if not await self._async_call(
                "zha",
                "set_zigbee_cluster_attribute",
                {
                    "ieee": valve.ieee,
                    "endpoint_id": 1,
                    "cluster_id": 0x000A,
                    "cluster_type": "in",
                    "attribute": attribute,
                    "value": value,
                },
            ):
                return False
        return True

    def valve_diagnostics(self, room: RoomDefinition) -> list[dict[str, Any]]:
        """Describe each valve's mode and last confirmed temperature write."""
        diagnostics: list[dict[str, Any]] = []
        for valve in room.valves:
            record = self._sync_records.get(valve.temperature_entity_id)
            time_record = self._sync_records.get(f"time:{valve.device_id}")
            covered = self.valve_covered(valve)
            diagnostics.append(
                {
                    "climate": valve.climate_entity_id,
                    "external_temperature": valve.temperature_entity_id,
                    "external_window": valve.window_entity_id,
                    "heat_available": valve.heat_available_entity_id,
                    "time_synced_at": time_record.succeeded_at.isoformat()
                    if time_record
                    else None,
                    "exercise_target": self.exercise_targets().get(valve.device_id),
                    "online": self.valve_online(valve),
                    "errors": self.valve_errors(valve),
                    "mode": None
                    if covered is None
                    else ("covered" if covered else "exposed"),
                    "last_sync_value": record.value if record else None,
                    "last_sync_at": record.succeeded_at.isoformat() if record else None,
                }
            )
        return diagnostics

    @property
    def sync_records(self) -> dict[str, SyncRecord]:
        """Expose a copy for persistence without allowing mutation."""
        return dict(self._sync_records)
