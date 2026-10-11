"""Home Assistant entry points for Danfoss Ally Coordinator."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AGGREGATION,
    CONF_AREAS,
    CONF_ENABLED,
    CONF_EXCLUDED_ENTITIES,
    CONF_HEAT_SOURCE,
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
from .entity_refs import to_entity_ids, to_registry_ids
from .store import SyncStateStore

_LOGGER = logging.getLogger(__name__)
RENAME_RELOAD_COOLDOWN = 10
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
    _migrate_stored_references(hass, entry)
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
        heat_source_entity_id=_heat_source(hass, configuration),
        **_live_settings(hass, configuration),
        sync_records=await store.async_load(),
        sync_state_store=store,
    )
    entry.runtime_data = coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator
    async_update_incomplete_valve_issues(hass, rooms)

    @callback
    def _persist_live_settings(settings: dict[str, list[str]]) -> None:
        """Store room-switch settings in the entry options (sources by registry ID)."""
        settings = {
            **settings,
            CONF_EXCLUDED_ENTITIES: to_registry_ids(
                hass, settings[CONF_EXCLUDED_ENTITIES]
            ),
        }
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, **settings}
        )

    coordinator.on_live_settings_changed = _persist_live_settings
    await _async_migrate_source_unique_ids(hass, entry)
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_orphans(hass, entry, coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(_async_track_renamed_entities(hass, entry, coordinator))
    return True


def _heat_source(hass: HomeAssistant, configuration: dict[str, Any]) -> str | None:
    """Resolve the stored heat source (registry ID) to its current entity ID."""
    if not (source := configuration.get(CONF_HEAT_SOURCE)):
        return None
    return next(iter(to_entity_ids(hass, [source])))


@callback
def _async_remove_orphans(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry, coordinator: RoomCoordinator
) -> None:
    """Drop the devices and entities of rooms that are no longer coordinated.

    A room that was deselected, or whose area was replaced (a new area ID after
    a rename), leaves its device and entities behind. Every entity of this
    integration is keyed by its room's area ID. Sources of a current room are
    left alone: one whose integration is still loading must keep its settings.
    """
    prefixes = tuple(f"{room.area_id}_" for room in coordinator.rooms)
    entity_registry = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        if not entity_entry.unique_id.startswith(prefixes):
            entity_registry.async_remove(entity_entry.entity_id)
    current_areas = {room.area_id for room in coordinator.rooms}
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        areas = {value for domain, value in device.identifiers if domain == DOMAIN}
        if areas and not areas & current_areas:
            device_registry.async_remove_device(device.id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: DanfossAllyConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Allow deleting a room device only once its room is no longer coordinated."""
    coordinator: RoomCoordinator | None = getattr(entry, "runtime_data", None)
    current = {room.area_id for room in coordinator.rooms} if coordinator else set()
    return not any(
        domain == DOMAIN and value in current for domain, value in device.identifiers
    )


@callback
def _migrate_stored_references(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry
) -> None:
    """Rewrite ignored sources stored as entity IDs to registry IDs (once)."""
    for source in (entry.data, entry.options):
        stored = source.get(CONF_EXCLUDED_ENTITIES)
        if stored is None:
            continue
        migrated = to_registry_ids(hass, stored)
        if migrated != sorted(stored):
            key = "data" if source is entry.data else "options"
            hass.config_entries.async_update_entry(
                entry, **{key: {**source, CONF_EXCLUDED_ENTITIES: migrated}}
            )


async def _async_migrate_source_unique_ids(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry
) -> None:
    """Re-key source switches from entity IDs to registry IDs (0.2.0 pre-release)."""
    registry = er.async_get(hass)

    @callback
    def _migrate(entity_entry: er.RegistryEntry) -> dict[str, str] | None:
        match = re.fullmatch(
            r"(.+)_source_((?:sensor|binary_sensor)\..+)", entity_entry.unique_id
        )
        if match and (source := registry.async_get(match.group(2))):
            return {"new_unique_id": f"{match.group(1)}_source_{source.id}"}
        return None

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


@callback
def _async_track_renamed_entities(
    hass: HomeAssistant, entry: DanfossAllyConfigEntry, coordinator: RoomCoordinator
) -> Callable[[], None]:
    """Reload once after a used entity got a new entity ID or another area.

    Rooms are discovered by entity ID; a reload picks up the new IDs while the
    stored settings follow by registry ID.
    """

    pending: list[Callable[[], None]] = []

    @callback
    def _reload(_now) -> None:
        pending.clear()
        hass.config_entries.async_schedule_reload(entry.entry_id)

    @callback
    def _handle(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update":
            return
        changes = data.get("changes", {})
        old_entity_id = data.get("old_entity_id", data["entity_id"])
        if ("entity_id" in changes or "area_id" in changes) and (
            old_entity_id in coordinator.known_entity_ids()
            or data["entity_id"] in coordinator.known_entity_ids()
        ):
            if not pending:
                # One reload for a whole batch of renames.
                pending.append(async_call_later(hass, RENAME_RELOAD_COOLDOWN, _reload))

    unsubscribe = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _handle)

    @callback
    def _stop() -> None:
        unsubscribe()
        for cancel in pending:
            cancel()
        pending.clear()

    return _stop


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


def _live_settings(
    hass: HomeAssistant, configuration: dict[str, Any]
) -> dict[str, frozenset[str]]:
    """Map stored room-switch settings to coordinator arguments."""
    return {
        "excluded_entity_ids": to_entity_ids(
            hass, configuration.get(CONF_EXCLUDED_ENTITIES, ())
        ),
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
        settings = _live_settings(hass, {**entry.data, **entry.options})
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
