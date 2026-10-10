"""Runtime coordinator for room-scoped Danfoss Ally automation."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .discovery import RoomDefinition
from .planner import (
    SyncRecord,
    TemperatureReading,
    average_temperature_celsius,
    should_push_temperature,
)
from .store import SyncStateStore

_LOGGER = logging.getLogger(__name__)
EVALUATION_INTERVAL = timedelta(minutes=5)


@dataclass(slots=True)
class RoomStatus:
    """Observable computed state for one configured HA area."""

    mean_temperature: float | None = None
    valid_temperature_sources: int = 0
    window_open: bool = False
    last_error: str | None = None
    last_reason: str | None = None


class RoomCoordinator:
    """Synchronize discovered room sources to typed, existing Danfoss ZHA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        rooms: tuple[RoomDefinition, ...],
        enabled: bool,
        max_sensor_age: timedelta,
        prioritize_external_sensor: bool = False,
        sync_temperature: bool = True,
        sync_windows: bool = True,
        sync_records: dict[str, SyncRecord] | None = None,
        sync_state_store: SyncStateStore | None = None,
    ) -> None:
        self.hass = hass
        self.rooms = rooms
        self.enabled = enabled
        self.max_sensor_age = max_sensor_age
        self.prioritize_external_sensor = prioritize_external_sensor
        self.sync_temperature = sync_temperature
        self.sync_windows = sync_windows
        self.statuses = {room.area_id: RoomStatus() for room in rooms}
        self._sync_records = sync_records or {}
        self._sync_state_store = sync_state_store
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self._evaluation_lock = asyncio.Lock()
        self._tracked_source_ids = self._source_entity_ids()

    def _source_entity_ids(self) -> frozenset[str]:
        """Return all entities whose update can change a room decision."""
        return frozenset(
            entity_id
            for room in self.rooms
            for entity_id in (
                *room.temperature_entity_ids,
                *room.window_entity_ids,
                *(
                    valve.priority_entity_id
                    for valve in room.valves
                    if valve.priority_entity_id
                ),
            )
        )

    async def async_start(self) -> None:
        """Start local event and cadence evaluation without polling Zigbee devices."""
        self._unsubscribers.append(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._async_handle_state_changed
            )
        )
        self._unsubscribers.append(
            async_track_time_interval(
                self.hass, self._async_handle_interval, EVALUATION_INTERVAL
            )
        )
        await self._async_evaluate_serial(reason="startup")

    async def async_stop(self) -> None:
        """Remove all listeners before the config entry unloads."""
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    @callback
    def _async_handle_state_changed(self, event) -> None:
        """Evaluate promptly for sources, never for own write targets."""
        if event.data.get("entity_id") in self._tracked_source_ids:
            self.hass.async_create_task(
                self._async_evaluate_serial(reason="source_state_changed")
            )

    async def _async_handle_interval(self, _now: datetime) -> None:
        """Keep the Danfoss external-sensor lease alive before it expires."""
        await self._async_evaluate_serial(reason="cadence")

    async def _async_evaluate_serial(self, *, reason: str) -> None:
        """Serialize service calls so concurrent HA state events cannot burst radios."""
        async with self._evaluation_lock:
            await self.async_evaluate_all(now=dt_util.utcnow(), reason=reason)

    @callback
    def async_replace_rooms(self, rooms: tuple[RoomDefinition, ...]) -> None:
        """Adopt fresh discovery while preserving unchanged room diagnostics."""
        previous_statuses = self.statuses
        self.rooms = rooms
        self.statuses = {
            room.area_id: previous_statuses.get(room.area_id, RoomStatus())
            for room in rooms
        }
        self._tracked_source_ids = self._source_entity_ids()
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
        """Turn HA states into planner inputs without trusting invalid state strings."""
        readings: list[TemperatureReading] = []
        for entity_id in room.temperature_entity_ids:
            if (state := self.hass.states.get(entity_id)) is None:
                continue
            try:
                value = float(state.state)
            except TypeError, ValueError:
                value = None
            readings.append(
                TemperatureReading(
                    entity_id=entity_id,
                    value=value,
                    unit=state.attributes.get("unit_of_measurement"),
                    observed_at=state.last_updated,
                )
            )
        return tuple(readings)

    @staticmethod
    def _count_valid_readings(
        readings: tuple[TemperatureReading, ...],
        *,
        now: datetime,
        max_age: timedelta,
    ) -> int:
        """Count exactly the fresh finite Celsius/Fahrenheit values used by the mean."""
        count = 0
        for reading in readings:
            if reading.value is None or now - reading.observed_at > max_age:
                continue
            if reading.unit in ("°C", "C"):
                celsius = reading.value
            elif reading.unit in ("°F", "F"):
                celsius = (reading.value - 32.0) * 5.0 / 9.0
            else:
                continue
            count += math.isfinite(celsius)
        return count

    def _room_window_open(self, room: RoomDefinition) -> bool:
        """A single active room contact is sufficient to request window-open mode."""
        return any(
            (state := self.hass.states.get(entity_id)) is not None
            and state.state == STATE_ON
            for entity_id in room.window_entity_ids
        )

    async def async_evaluate_all(self, *, now: datetime, reason: str) -> None:
        """Evaluate every room and durably save any successful temperature write."""
        records_before = self._sync_records.copy()
        for room in self.rooms:
            await self.async_evaluate_room(room=room, now=now, reason=reason)
        if self._sync_state_store is not None and records_before != self._sync_records:
            await self._sync_state_store.async_save(self._sync_records)

    async def async_evaluate_room(
        self, *, room: RoomDefinition, now: datetime, reason: str
    ) -> None:
        """Calculate one room's state and issue only required typed service calls."""
        status = self.statuses[room.area_id]
        readings = self._temperature_readings(room)
        mean = average_temperature_celsius(
            readings, now=now, max_age=self.max_sensor_age
        )
        window_open = self._room_window_open(room)
        status.mean_temperature = mean
        status.valid_temperature_sources = self._count_valid_readings(
            readings, now=now, max_age=self.max_sensor_age
        )
        status.window_open = window_open
        status.last_error = None
        status.last_reason = reason

        if not self.enabled:
            self._notify_listeners()
            return

        for valve in room.valves:
            if valve.priority_entity_id:
                priority_state = self.hass.states.get(valve.priority_entity_id)
                desired_priority = (
                    self.sync_temperature
                    and self.prioritize_external_sensor
                    and mean is not None
                )
                desired_priority_state = STATE_ON if desired_priority else "off"
                if (
                    priority_state is None
                    or priority_state.state != desired_priority_state
                ):
                    try:
                        await self.hass.services.async_call(
                            "switch",
                            SERVICE_TURN_ON if desired_priority else SERVICE_TURN_OFF,
                            {ATTR_ENTITY_ID: valve.priority_entity_id},
                            blocking=True,
                        )
                    except HomeAssistantError as err:
                        status.last_error = f"{valve.priority_entity_id}: {err}"
                        _LOGGER.warning(
                            "External sensor priority fallback failed: %s", err
                        )
                    else:
                        readback = self.hass.states.get(valve.priority_entity_id)
                        if readback is None or readback.state != desired_priority_state:
                            status.last_error = (
                                f"{valve.priority_entity_id}: priority readback "
                                f"mismatch (expected {desired_priority_state}, got "
                                f"{readback.state if readback else None})"
                            )
                            _LOGGER.warning(
                                "Danfoss priority readback mismatch: %s",
                                status.last_error,
                            )

            if self.sync_temperature and mean is not None:
                sent_value = round(mean, 1)
                if should_push_temperature(
                    value=sent_value,
                    last_sync=self._sync_records.get(valve.temperature_entity_id),
                    now=now,
                    covered=False,
                ):
                    try:
                        await self.hass.services.async_call(
                            "number",
                            "set_value",
                            {
                                ATTR_ENTITY_ID: valve.temperature_entity_id,
                                "value": sent_value,
                            },
                            blocking=True,
                        )
                    except HomeAssistantError as err:
                        status.last_error = f"{valve.temperature_entity_id}: {err}"
                        _LOGGER.warning("Danfoss temperature sync failed: %s", err)
                    else:
                        state = self.hass.states.get(valve.temperature_entity_id)
                        try:
                            readback = float(state.state) if state is not None else None
                        except TypeError, ValueError:
                            readback = None
                        if readback is None or abs(readback - sent_value) > 0.05:
                            status.last_error = (
                                f"{valve.temperature_entity_id}: readback mismatch "
                                f"(expected {sent_value}, got {readback})"
                            )
                            _LOGGER.warning(
                                "Danfoss temperature readback mismatch: %s",
                                status.last_error,
                            )
                        else:
                            self._sync_records[valve.temperature_entity_id] = (
                                SyncRecord(
                                    value=sent_value,
                                    succeeded_at=now,
                                )
                            )

            if not self.sync_windows:
                continue
            desired_state = STATE_ON if window_open else "off"
            current_state = self.hass.states.get(valve.window_entity_id)
            if current_state is not None and current_state.state == desired_state:
                continue
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
            else:
                readback = self.hass.states.get(valve.window_entity_id)
                if readback is None or readback.state != desired_state:
                    status.last_error = (
                        f"{valve.window_entity_id}: window readback mismatch "
                        f"(expected {desired_state}, got "
                        f"{readback.state if readback else None})"
                    )
                    _LOGGER.warning(
                        "Danfoss window readback mismatch: %s", status.last_error
                    )

        self._notify_listeners()

    @property
    def sync_records(self) -> dict[str, SyncRecord]:
        """Expose a copy for persistence without allowing mutation."""
        return dict(self._sync_records)
