"""House-wide entities: heat demand signals across all coordinated rooms.

They only report. Nothing in the heating system is switched from them, so the
district heating station is never asked for more heat than it already supplies.
"""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .coordinator import RoomCoordinator

HOUSE_ID = "house"


class DanfossAllyHouseEntity(Entity):
    """An entity on the house device, recalculated on every source change."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: RoomCoordinator, suffix: str) -> None:
        self.coordinator = coordinator
        # The "house_" prefix keeps it clear of the room cleanup at setup.
        self._attr_unique_id = f"{HOUSE_ID}_{suffix}"
        self._attr_translation_key = suffix
        self._attr_device_info = {
            "identifiers": {(DOMAIN, HOUSE_ID)},
            "name": "Danfoss Ally – Haus",
            "manufacturer": "Danfoss Ally Coordinator",
            "model": "House",
        }

    async def async_added_to_hass(self) -> None:
        """Follow the valves' and thermostats' demand as it changes."""

        @callback
        def _changed(_event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, sorted(self.coordinator.demand_entity_ids()), _changed
            )
        )
