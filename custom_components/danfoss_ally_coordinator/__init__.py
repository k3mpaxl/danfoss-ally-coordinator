"""Home Assistant entry points for Danfoss Ally Coordinator."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AGGREGATION,
    CONF_AREAS,
    CONF_ENABLED,
    CONF_EXCLUDED_ENTITIES,
    CONF_LOAD_BALANCING_AREAS,
    CONF_MAX_SENSOR_AGE_MINUTES,
    CONF_SETPOINT_SYNC_DISABLED_AREAS,
    CONF_STARTUP_DELAY_MINUTES,
    CONF_SYNC_TEMPERATURE,
    CONF_SYNC_VALVE_WINDOWS,
    CONF_SYNC_WINDOWS,
    DEFAULT_AGGREGATION,
    DEFAULT_STARTUP_DELAY_MINUTES,
    DEFAULT_SYNC_TEMPERATURE,
    DEFAULT_SYNC_VALVE_WINDOWS,
    DEFAULT_SYNC_WINDOWS,
    DOMAIN,
    LIVE_OPTION_KEYS,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from .coordinator import RoomCoordinator
from .discovery import RoomDefinition, async_discover_rooms
from .store import SyncStateStore

_LOGGER = logging.getLogger(__name__)
PLATFORMS: tuple[Platform, ...] = (
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type DanfossAllyConfigEntry = ConfigEntry[RoomCoordinator]


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Set up global services once; entries provide the room coordinators."""
    hass.data.setdefault(DOMAIN, {})

    async def async_handle_refresh(_call: ServiceCall) -> None:
        """Rediscover areas, then evaluate current source membership."""
        coordinators = tuple(hass.data[DOMAIN].values())
        await asyncio.gather(
            *(
                coordinator.async_refresh(
                    lambda coordinator=coordinator: async_discover_rooms(
                        hass, {room.area_id for room in coordinator.rooms}
                    )
                )
                for coordinator in coordinators
            )
        )
        async_update_incomplete_valve_issues(
            hass,
            tuple(room for coordinator in coordinators for room in coordinator.rooms),
        )

    hass.services.async_register(
        DOMAIN,
        "refresh",
        async_handle_refresh,
        schema=vol.Schema({}),
    )

    async def async_handle_calibration(call: ServiceCall) -> ServiceResponse:
        """Only an explicit service action may change one valve's local calibration."""
        entity_id = call.data["entity_id"]
        for coordinator in tuple(hass.data[DOMAIN].values()):
            if any(
                valve.climate_entity_id == entity_id
                for room in coordinator.rooms
                for valve in room.valves
            ):
                return await coordinator.async_calibrate_local_sensor(
                    entity_id, now=dt_util.utcnow()
                )
        raise HomeAssistantError(
            "Selected climate entity is not a coordinated Danfoss valve"
        )

    hass.services.async_register(
        DOMAIN,
        "calibrate_local_sensor",
        async_handle_calibration,
        schema=vol.Schema({vol.Required("entity_id"): cv.entity_id}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DanfossAllyConfigEntry) -> bool:
    """Set up one explicitly selected set of discovered HA areas."""
    configuration = {**entry.data, **entry.options}
    selected_area_ids = set(configuration[CONF_AREAS])
    rooms = await async_discover_rooms(hass, selected_area_ids)
    if not rooms:
        raise ConfigEntryNotReady(
            "No controllable ZHA Danfoss eTRVs found in the selected areas"
        )

    store = SyncStateStore(
        Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}.{entry.entry_id}",
        )
    )
    coordinator = RoomCoordinator(
        hass,
        configuration=_reload_relevant(entry),
        rooms=rooms,
        enabled=bool(configuration[CONF_ENABLED]),
        max_sensor_age=timedelta(
            minutes=int(configuration[CONF_MAX_SENSOR_AGE_MINUTES])
        ),
        sync_temperature=bool(
            configuration.get(CONF_SYNC_TEMPERATURE, DEFAULT_SYNC_TEMPERATURE)
        ),
        sync_windows=bool(configuration.get(CONF_SYNC_WINDOWS, DEFAULT_SYNC_WINDOWS)),
        sync_valve_windows=bool(
            configuration.get(CONF_SYNC_VALVE_WINDOWS, DEFAULT_SYNC_VALVE_WINDOWS)
        ),
        aggregation=configuration.get(CONF_AGGREGATION, DEFAULT_AGGREGATION),
        startup_delay=timedelta(
            minutes=int(
                configuration.get(
                    CONF_STARTUP_DELAY_MINUTES, DEFAULT_STARTUP_DELAY_MINUTES
                )
            )
        ),
        **_live_settings(configuration),
        sync_records=await store.async_load(),
        sync_state_store=store,
    )
    entry.runtime_data = coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator
    async_update_incomplete_valve_issues(hass, rooms)

    @callback
    def _persist_live_settings(settings: dict[str, list[str]]) -> None:
        """Store room-switch settings in the entry options."""
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **settings}
        )

    coordinator.on_live_settings_changed = _persist_live_settings
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


@callback
def async_update_incomplete_valve_issues(
    hass: HomeAssistant, rooms: tuple[RoomDefinition, ...]
) -> None:
    """Raise a repair issue for each valve ZHA exposes without control entities."""
    current: set[str] = set()
    for room in rooms:
        for valve in room.incomplete_valves:
            issue_id = f"incomplete_valve_{valve.device_id}"
            current.add(issue_id)
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="incomplete_valve",
                translation_placeholders={
                    "valve": valve.name or valve.device_id,
                    "area": room.name,
                    "missing": ", ".join(valve.missing),
                },
            )
    for domain, issue_id in tuple(ir.async_get(hass).issues):
        if (
            domain == DOMAIN
            and issue_id.startswith("incomplete_valve_")
            and issue_id not in current
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _reload_relevant(entry: DanfossAllyConfigEntry) -> dict[str, Any]:
    """Return every setting except those the room switches apply live."""
    configuration = {**entry.data, **entry.options}
    for key in LIVE_OPTION_KEYS:
        configuration.pop(key, None)
    return configuration


def _live_settings(configuration: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Map stored room-switch settings to coordinator arguments."""
    return {
        "excluded_entity_ids": frozenset(configuration.get(CONF_EXCLUDED_ENTITIES, ())),
        "setpoint_sync_disabled_areas": frozenset(
            configuration.get(CONF_SETPOINT_SYNC_DISABLED_AREAS, ())
        ),
        "load_balancing_areas": frozenset(
            configuration.get(CONF_LOAD_BALANCING_AREAS, ())
        ),
    }


async def _async_options_updated(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry
) -> None:
    """Apply room-switch settings live; reload for any other changed setting."""
    coordinator = entry.runtime_data
    if _reload_relevant(entry) == coordinator.configuration:
        settings = _live_settings({**entry.data, **entry.options})
        await coordinator.async_apply_live_settings(
            excluded=settings["excluded_entity_ids"],
            setpoint_sync_disabled=settings["setpoint_sync_disabled_areas"],
            load_balancing=settings["load_balancing_areas"],
        )
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry
) -> bool:
    """Unload entities and stop local listeners for one config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        async_update_incomplete_valve_issues(hass, ())
    return unloaded
