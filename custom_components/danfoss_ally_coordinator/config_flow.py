"""UI configuration for room-scoped Danfoss Ally coordination."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_AREAS,
    CONF_ENABLED,
    CONF_MAX_SENSOR_AGE_MINUTES,
    CONF_PRIORITIZE_EXTERNAL_SENSOR,
    CONF_SYNC_TEMPERATURE,
    CONF_SYNC_WINDOWS,
    DEFAULT_ENABLED,
    DEFAULT_MAX_SENSOR_AGE_MINUTES,
    DEFAULT_PRIORITIZE_EXTERNAL_SENSOR,
    DEFAULT_SYNC_TEMPERATURE,
    DEFAULT_SYNC_WINDOWS,
    DOMAIN,
    MAX_SENSOR_AGE_MINUTES,
    MIN_SENSOR_AGE_MINUTES,
    NAME,
)
from .discovery import async_discover_rooms


def _schema(rooms, defaults: dict[str, Any]) -> vol.Schema:
    """Build an area selector from live, eligible ZHA Danfoss room groups."""
    area_options = [
        selector.SelectOptionDict(
            value=room.area_id,
            label=(
                f"{room.name} ({len(room.valves)} Danfoss eTRV, "
                f"{len(room.temperature_entity_ids)} Temperatursensoren, "
                f"{len(room.window_entity_ids)} Öffnungskontakte)"
            ),
        )
        for room in rooms
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_AREAS,
                default=defaults.get(CONF_AREAS, [room.area_id for room in rooms]),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=area_options, multiple=True)
            ),
            vol.Required(
                CONF_ENABLED,
                default=defaults.get(CONF_ENABLED, DEFAULT_ENABLED),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_MAX_SENSOR_AGE_MINUTES,
                default=defaults.get(
                    CONF_MAX_SENSOR_AGE_MINUTES, DEFAULT_MAX_SENSOR_AGE_MINUTES
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_SENSOR_AGE_MINUTES,
                    max=MAX_SENSOR_AGE_MINUTES,
                    step=5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Required(
                CONF_PRIORITIZE_EXTERNAL_SENSOR,
                default=defaults.get(
                    CONF_PRIORITIZE_EXTERNAL_SENSOR,
                    DEFAULT_PRIORITIZE_EXTERNAL_SENSOR,
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_SYNC_TEMPERATURE,
                default=defaults.get(CONF_SYNC_TEMPERATURE, DEFAULT_SYNC_TEMPERATURE),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_SYNC_WINDOWS,
                default=defaults.get(CONF_SYNC_WINDOWS, DEFAULT_SYNC_WINDOWS),
            ): selector.BooleanSelector(),
        }
    )


async def _discover_options(hass):
    """Discover every area first, then let the user explicitly select its rooms."""
    from homeassistant.helpers import area_registry as ar

    all_area_ids = set(ar.async_get(hass).areas)
    return await async_discover_rooms(hass, all_area_ids)


class DanfossAllyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up exactly one coordinator through the normal HA UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Discover eligible room candidates, then store an explicit selection."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        rooms = await _discover_options(self.hass)
        if not rooms:
            return self.async_abort(reason="no_supported_rooms")
        if user_input is not None:
            return self.async_create_entry(title=NAME, data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema(rooms, {}))

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> DanfossAllyOptionsFlow:
        """Expose the same discovery-aware settings after setup."""
        return DanfossAllyOptionsFlow(config_entry)


class DanfossAllyOptionsFlow(config_entries.OptionsFlow):
    """Reconfigure selected areas and opt in/out of radio writes."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Refresh candidates before rendering the options form."""
        rooms = await _discover_options(self.hass)
        if not rooms:
            return self.async_abort(reason="no_supported_rooms")
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_schema(rooms, defaults)
        )
