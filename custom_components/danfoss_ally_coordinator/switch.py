"""Per-room switches that show and control which sources feed a room."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DanfossAllyConfigEntry
from .coordinator import RoomCoordinator
from .discovery import RoomDefinition
from .entity import DanfossAllyRoomEntity
from .entity_refs import registry_id

KIND_TEMPERATURE = "temperature"
KIND_WINDOW = "window"

ICONS = {
    (KIND_TEMPERATURE, "used"): "mdi:thermometer-check",
    (KIND_TEMPERATURE, "ignored"): "mdi:thermometer-off",
    (KIND_WINDOW, "open"): "mdi:window-open-variant",
    (KIND_WINDOW, "closed"): "mdi:window-closed-variant",
    (KIND_WINDOW, "ignored"): "mdi:window-shutter-cog",
}
FALLBACK_ICONS = {
    KIND_TEMPERATURE: "mdi:thermometer-alert",
    KIND_WINDOW: "mdi:window-shutter-alert",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DanfossAllyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create room feature switches and one switch per discovered source."""
    coordinator = entry.runtime_data
    async_add_entities(
        RoomSetpointSyncSwitch(coordinator, room)
        for room in coordinator.rooms
        if sum(bool(valve.climate_entity_id) for valve in room.valves) >= 2
    )
    async_add_entities(
        RoomLoadBalancingSwitch(coordinator, room)
        for room in coordinator.rooms
        if coordinator.load_balancing_supported(room)
    )
    async_add_entities(
        RoomSourceSwitch(coordinator, room, entity_id, kind)
        for room in coordinator.rooms
        for kind, entity_ids in (
            (KIND_TEMPERATURE, room.temperature_entity_ids),
            (KIND_WINDOW, room.window_entity_ids),
        )
        for entity_id in entity_ids
    )


class RoomSetpointSyncSwitch(DanfossAllyRoomEntity, SwitchEntity):
    """On while a change at one valve sets the same target on the room's others."""

    _entity_suffix = "setpoint_sync"
    _attr_translation_key = "setpoint_sync"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermostat-box"

    @property
    def is_on(self) -> bool:
        return self.coordinator.setpoint_sync_enabled(self.room)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_setpoint_sync(self.room.area_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_setpoint_sync(self.room.area_id, False)


class RoomLoadBalancingSwitch(DanfossAllyRoomEntity, SwitchEntity):
    """On while the room's mean radiator load is shared between its valves."""

    _entity_suffix = "load_balancing"
    _attr_translation_key = "load_balancing"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:scale-balance"

    @property
    def is_on(self) -> bool:
        return self.coordinator.load_balancing_active(self.room)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show each valve's load estimate and the mean last sent to it."""
        return {
            **super().extra_state_attributes,
            **self.coordinator.load_diagnostics(self.room),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_load_balancing(self.room.area_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_load_balancing(self.room.area_id, False)


class RoomSourceSwitch(DanfossAllyRoomEntity, SwitchEntity):
    """On while a discovered sensor or contact is used for its room.

    The icon shows the source's current status: used, ignored, or unusable
    (unavailable, stale or an unsupported unit).
    """

    _entity_suffix = "source"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: RoomCoordinator,
        room: RoomDefinition,
        source_entity_id: str,
        kind: str,
    ) -> None:
        super().__init__(coordinator, room)
        self.source_entity_id = source_entity_id
        self.kind = kind
        # Keyed by registry ID so a renamed source keeps its switch.
        self._attr_unique_id = (
            f"{room.area_id}_source_{registry_id(coordinator.hass, source_entity_id)}"
        )
        self._update_from_coordinator()

    async def async_added_to_hass(self) -> None:
        """Refresh name, icon and status whenever the room is re-evaluated."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self._async_handle_update)
        )

    @callback
    def _async_handle_update(self) -> None:
        self._update_from_coordinator()
        self.async_write_ha_state()

    def _source_name(self) -> str:
        """Use the source's own name without the repeated room name."""
        hass = self.coordinator.hass
        name: str | None = None
        if (state := hass.states.get(self.source_entity_id)) is not None:
            name = state.name
        elif entry := er.async_get(hass).async_get(self.source_entity_id):
            name = entry.name or entry.original_name
        name = name or self.source_entity_id
        prefix = f"{self.room.name} "
        if name.casefold().startswith(prefix.casefold()) and len(name) > len(prefix):
            name = name[len(prefix) :]
        return name

    def _status(self) -> str:
        details = self.coordinator.source_details(self.room)[self.kind]
        return next(
            (
                detail["status"]
                for detail in details
                if detail["entity_id"] == self.source_entity_id
            ),
            "unavailable",
        )

    def _update_from_coordinator(self) -> None:
        status = self._status()
        self._attr_name = self._source_name()
        self._attr_is_on = (
            self.source_entity_id not in self.coordinator.excluded_entity_ids
        )
        self._attr_icon = ICONS.get((self.kind, status), FALLBACK_ICONS[self.kind])
        self._attr_extra_state_attributes = {
            "source_entity_id": self.source_entity_id,
            "kind": self.kind,
            "status": status,
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the source and why it is or is not used."""
        return self._attr_extra_state_attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Use this source for the room again."""
        await self.coordinator.async_set_source_enabled(self.source_entity_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Ignore this source for the room."""
        await self.coordinator.async_set_source_enabled(self.source_entity_id, False)
