"""UI configuration for room-scoped Danfoss Ally coordination."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    AGGREGATION_MEAN,
    AGGREGATION_MEDIAN,
    CONF_ADAPTATION_WITH_HEAT,
    CONF_AGGREGATION,
    CONF_AREAS,
    CONF_DEMAND_CLIMATES,
    CONF_ENABLED,
    CONF_EXCLUDED_ENTITIES,
    CONF_EXERCISE_DAY,
    CONF_EXERCISE_TIME,
    CONF_HEAT_SOURCE,
    CONF_MAX_SENSOR_AGE_MINUTES,
    CONF_STARTUP_DELAY_MINUTES,
    CONF_SYNC_TEMPERATURE,
    CONF_SYNC_VALVE_WINDOWS,
    CONF_SYNC_WINDOWS,
    DEFAULT_ADAPTATION_WITH_HEAT,
    DEFAULT_AGGREGATION,
    DEFAULT_ENABLED,
    DEFAULT_EXERCISE_DAY,
    DEFAULT_EXERCISE_TIME,
    DEFAULT_MAX_SENSOR_AGE_MINUTES,
    DEFAULT_STARTUP_DELAY_MINUTES,
    DEFAULT_SYNC_TEMPERATURE,
    DEFAULT_SYNC_VALVE_WINDOWS,
    DEFAULT_SYNC_WINDOWS,
    DOMAIN,
    EXERCISE_OFF,
    MAX_SENSOR_AGE_MINUTES,
    MAX_STARTUP_DELAY_MINUTES,
    MIN_SENSOR_AGE_MINUTES,
    NAME,
)
from .discovery import async_discover_rooms
from .entity_refs import to_entity_ids, to_registry_ids
from .maintenance import EXERCISE_DAYS


def _area_option_label(room) -> str:
    """Show discovery counts without binding labels to one UI language.

    `⚠` counts valves that ZHA exposes without the required control entities.
    """
    incomplete = len(getattr(room, "incomplete_valves", ()))
    return (
        f"{room.name} (TRV {len(room.valves)}"
        + (f" ⚠ {incomplete}" if incomplete else "")
        + f" · T {len(room.temperature_entity_ids)}"
        + f" · W {len(room.window_entity_ids)})"
    )


def _schema(rooms, defaults: dict[str, Any]) -> vol.Schema:
    """Build an area selector from live, eligible ZHA Danfoss room groups."""
    area_options = [
        selector.SelectOptionDict(
            value=room.area_id,
            label=_area_option_label(room),
        )
        for room in rooms
    ]
    available_area_ids = [room.area_id for room in rooms]
    # A previously selected area may no longer contain a controllable valve.
    selected_area_ids = [
        area_id
        for area_id in defaults.get(CONF_AREAS, available_area_ids)
        if area_id in available_area_ids
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_AREAS,
                default=selected_area_ids,
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
                CONF_STARTUP_DELAY_MINUTES,
                default=defaults.get(
                    CONF_STARTUP_DELAY_MINUTES, DEFAULT_STARTUP_DELAY_MINUTES
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=MAX_STARTUP_DELAY_MINUTES,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Required(
                CONF_SYNC_TEMPERATURE,
                default=defaults.get(CONF_SYNC_TEMPERATURE, DEFAULT_SYNC_TEMPERATURE),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_SYNC_WINDOWS,
                default=defaults.get(CONF_SYNC_WINDOWS, DEFAULT_SYNC_WINDOWS),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_SYNC_VALVE_WINDOWS,
                default=defaults.get(
                    CONF_SYNC_VALVE_WINDOWS, DEFAULT_SYNC_VALVE_WINDOWS
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_AGGREGATION,
                default=defaults.get(CONF_AGGREGATION, DEFAULT_AGGREGATION),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[AGGREGATION_MEDIAN, AGGREGATION_MEAN],
                    translation_key=CONF_AGGREGATION,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(
                CONF_EXCLUDED_ENTITIES,
                default=list(defaults.get(CONF_EXCLUDED_ENTITIES, [])),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["sensor", "binary_sensor"], multiple=True
                )
            ),
            vol.Optional(
                CONF_HEAT_SOURCE,
                description={"suggested_value": defaults.get(CONF_HEAT_SOURCE)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "switch", "input_boolean"]
                )
            ),
            vol.Required(
                CONF_ADAPTATION_WITH_HEAT,
                default=defaults.get(
                    CONF_ADAPTATION_WITH_HEAT, DEFAULT_ADAPTATION_WITH_HEAT
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_DEMAND_CLIMATES,
                default=list(defaults.get(CONF_DEMAND_CLIMATES, [])),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Required(
                CONF_EXERCISE_DAY,
                default=defaults.get(CONF_EXERCISE_DAY, DEFAULT_EXERCISE_DAY),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[EXERCISE_OFF, *EXERCISE_DAYS],
                    translation_key=CONF_EXERCISE_DAY,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_EXERCISE_TIME,
                default=defaults.get(CONF_EXERCISE_TIME, DEFAULT_EXERCISE_TIME),
            ): selector.TimeSelector(),
        }
    )


def _store_references(hass, user_input: dict[str, Any]) -> dict[str, Any]:
    """Persist entity references by registry ID so entity renames keep them.

    A cleared heat source is stored as ``None``: the form leaves an emptied
    optional field out, which would otherwise keep the old choice.
    """
    stored = {**user_input, CONF_HEAT_SOURCE: None}
    if source := user_input.get(CONF_HEAT_SOURCE):
        stored[CONF_HEAT_SOURCE] = to_registry_ids(hass, [source])[0]
    for key in (CONF_EXCLUDED_ENTITIES, CONF_DEMAND_CLIMATES):
        if key in user_input:
            stored[key] = to_registry_ids(hass, user_input[key])
    return stored


def _show_references(hass, configuration: dict[str, Any]) -> dict[str, Any]:
    """Show stored registry IDs as the current entity IDs in the form."""
    source = configuration.get(CONF_HEAT_SOURCE)
    return {
        **configuration,
        CONF_EXCLUDED_ENTITIES: sorted(
            to_entity_ids(hass, configuration.get(CONF_EXCLUDED_ENTITIES, ()))
        ),
        CONF_DEMAND_CLIMATES: sorted(
            to_entity_ids(hass, configuration.get(CONF_DEMAND_CLIMATES, ()))
        ),
        CONF_HEAT_SOURCE: next(iter(to_entity_ids(hass, [source]))) if source else None,
    }


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    """Reject a configuration that would coordinate nothing."""
    if not user_input.get(CONF_AREAS):
        return {CONF_AREAS: "no_areas_selected"}
    return {}


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
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (errors := _validate(user_input)):
                return self.async_create_entry(
                    title=NAME, data=_store_references(self.hass, user_input)
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(rooms, user_input or {}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> DanfossAllyOptionsFlow:
        """Expose the same discovery-aware settings after setup."""
        return DanfossAllyOptionsFlow()


class DanfossAllyOptionsFlow(config_entries.OptionsFlow):
    """Reconfigure selected areas and opt in/out of radio writes."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Refresh candidates before rendering the options form."""
        rooms = await _discover_options(self.hass)
        if not rooms:
            return self.async_abort(reason="no_supported_rooms")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not (errors := _validate(user_input)):
                # Keep settings made with the room switches.
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        **_store_references(self.hass, user_input),
                    },
                )
        defaults = user_input or _show_references(
            self.hass, {**self.config_entry.data, **self.config_entry.options}
        )
        return self.async_show_form(
            step_id="init", data_schema=_schema(rooms, defaults), errors=errors
        )
