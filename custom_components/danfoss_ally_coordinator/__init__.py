"""Home Assistant entry points for Danfoss Ally Coordinator."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AREAS,
    CONF_ENABLED,
    CONF_MAX_SENSOR_AGE_MINUTES,
    CONF_PRIORITIZE_EXTERNAL_SENSOR,
    CONF_SYNC_TEMPERATURE,
    CONF_SYNC_WINDOWS,
    DOMAIN,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from .coordinator import RoomCoordinator
from .discovery import async_discover_rooms
from .store import SyncStateStore

_LOGGER = logging.getLogger(__name__)
PLATFORMS: tuple[Platform, ...] = (Platform.SENSOR, Platform.BINARY_SENSOR)

type DanfossAllyConfigEntry = ConfigEntry[RoomCoordinator]


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Set up global services once; entries provide the room coordinators."""
    hass.data.setdefault(DOMAIN, {})

    async def async_handle_refresh(_call: ServiceCall) -> None:
        """Rediscover areas, then evaluate current source membership."""
        coordinators = tuple(hass.data[DOMAIN].values())
        for coordinator in coordinators:
            selected_area_ids = {room.area_id for room in coordinator.rooms}
            refreshed_rooms = await async_discover_rooms(hass, selected_area_ids)
            if refreshed_rooms:
                coordinator.async_replace_rooms(refreshed_rooms)
            else:
                _LOGGER.warning(
                    "Skipping empty Danfoss rediscovery; preserving the previous set"
                )
        await asyncio.gather(
            *(
                coordinator.async_evaluate_all(
                    now=dt_util.utcnow(), reason="manual_refresh"
                )
                for coordinator in coordinators
            )
        )

    hass.services.async_register(
        DOMAIN,
        "refresh",
        async_handle_refresh,
        schema=vol.Schema({}),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DanfossAllyConfigEntry) -> bool:
    """Set up one explicitly selected set of discovered HA areas."""
    configuration = {**entry.data, **entry.options}
    selected_area_ids = set(configuration[CONF_AREAS])
    rooms = await async_discover_rooms(hass, selected_area_ids)
    if not rooms:
        _LOGGER.error("No controllable ZHA Danfoss eTRVs found in selected areas")
        return False

    store = SyncStateStore(
        Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}.{entry.entry_id}",
        )
    )
    coordinator = RoomCoordinator(
        hass,
        rooms=rooms,
        enabled=bool(configuration[CONF_ENABLED]),
        max_sensor_age=timedelta(
            minutes=int(configuration[CONF_MAX_SENSOR_AGE_MINUTES])
        ),
        prioritize_external_sensor=bool(
            configuration.get(CONF_PRIORITIZE_EXTERNAL_SENSOR, False)
        ),
        sync_temperature=bool(configuration.get(CONF_SYNC_TEMPERATURE, True)),
        sync_windows=bool(configuration.get(CONF_SYNC_WINDOWS, True)),
        sync_records=await store.async_load(),
        sync_state_store=store,
    )
    entry.runtime_data = coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry
) -> None:
    """Reload with updated room selection or write-safety settings."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry
) -> bool:
    """Unload entities and stop local listeners for one config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
