"""Pure registry-discovery rules for Danfoss Ally room coordinators."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

DANFOSS_MANUFACTURER = "Danfoss"
DANFOSS_MODEL = "eTRV0103"
ZHA_PLATFORM = "zha"
TEMPERATURE_UNIQUE_SUFFIX = "-external_measured_room_sensor"
WINDOW_UNIQUE_SUFFIX = "-external_open_window_detected"
COVERED_UNIQUE_SUFFIX = "-radiator_covered"
WINDOW_DEVICE_CLASSES = frozenset({"window", "opening", "door"})


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """The registry properties needed to classify one HA device."""

    device_id: str
    manufacturer: str | None
    model: str | None
    area_id: str | None


@dataclass(frozen=True, slots=True)
class EntityRecord:
    """The registry properties needed to assign one HA entity to a room."""

    entity_id: str
    domain: str
    platform: str
    unique_id: str
    device_id: str | None
    area_id: str | None
    device_class: str | None
    disabled: bool = False
    translation_key: str | None = None


@dataclass(frozen=True, slots=True)
class ValveTarget:
    """The existing ZHA entities used to control one Danfoss eTRV."""

    device_id: str
    temperature_entity_id: str
    window_entity_id: str
    priority_entity_id: str | None


@dataclass(frozen=True, slots=True)
class RoomDefinition:
    """All discovered eTRV targets and source entities for one HA area."""

    area_id: str
    name: str
    valves: tuple[ValveTarget, ...]
    temperature_entity_ids: tuple[str, ...]
    window_entity_ids: tuple[str, ...]


def _entity_area(entity: EntityRecord, devices: dict[str, DeviceRecord]) -> str | None:
    """Use entity assignment first, then the parent device's area."""
    if entity.area_id:
        return entity.area_id
    if entity.device_id and (device := devices.get(entity.device_id)):
        return device.area_id
    return None


def _zha_danfoss_target_ids(
    devices: dict[str, DeviceRecord], entities: tuple[EntityRecord, ...]
) -> set[str]:
    """Find only Danfoss eTRVs that expose a ZHA Climate entity."""
    zha_climate_device_ids = {
        entity.device_id
        for entity in entities
        if not entity.disabled
        and entity.domain == "climate"
        and entity.platform == ZHA_PLATFORM
        and entity.device_id is not None
    }
    return {
        device_id
        for device_id, device in devices.items()
        if device_id in zha_climate_device_ids
        and device.manufacturer == DANFOSS_MANUFACTURER
        and device.model == DANFOSS_MODEL
    }


def _find_target(
    device_id: str, entities: tuple[EntityRecord, ...]
) -> ValveTarget | None:
    """Find the typed ZHA control entities without depending on translated names."""
    device_entities = [
        entity
        for entity in entities
        if entity.device_id == device_id
        and entity.platform == ZHA_PLATFORM
        and not entity.disabled
    ]
    temperature = next(
        (
            entity.entity_id
            for entity in device_entities
            if entity.domain == "number"
            and entity.unique_id.endswith(TEMPERATURE_UNIQUE_SUFFIX)
        ),
        None,
    )
    window = next(
        (
            entity.entity_id
            for entity in device_entities
            if entity.domain == "switch"
            and entity.unique_id.endswith(WINDOW_UNIQUE_SUFFIX)
        ),
        None,
    )
    priority = next(
        (
            entity.entity_id
            for entity in device_entities
            if entity.domain == "switch"
            and entity.unique_id.endswith(COVERED_UNIQUE_SUFFIX)
        ),
        None,
    )
    if temperature is None or window is None:
        return None
    return ValveTarget(device_id, temperature, window, priority)


def discover_rooms(
    *,
    devices: Iterable[DeviceRecord],
    entities: Iterable[EntityRecord],
    areas: dict[str, str],
    selected_area_ids: set[str],
) -> tuple[RoomDefinition, ...]:
    """Discover ZHA Danfoss room groups and all safe area-scoped sources.

    An area is a configuration hint, not proof of a physical room. The caller asks
    the user to select the discovered areas before writes can be enabled.
    """
    device_map = {device.device_id: device for device in devices}
    all_entities = tuple(entities)
    targets = _zha_danfoss_target_ids(device_map, all_entities)
    valves_by_area: dict[str, list[ValveTarget]] = {}
    for target_id in targets:
        device = device_map[target_id]
        area_id = device.area_id
        if area_id is None or area_id not in selected_area_ids:
            continue
        if valve := _find_target(target_id, all_entities):
            valves_by_area.setdefault(area_id, []).append(valve)

    rooms: list[RoomDefinition] = []
    for area_id in sorted(valves_by_area):
        area_targets = frozenset(valve.device_id for valve in valves_by_area[area_id])
        temperatures: list[str] = []
        windows: list[str] = []
        for entity in all_entities:
            if entity.disabled or entity.platform == "danfoss_ally_coordinator":
                continue
            if entity.device_id in area_targets:
                continue
            if _entity_area(entity, device_map) != area_id:
                continue
            if (
                entity.domain == "sensor"
                and entity.device_class == "temperature"
                and entity.translation_key != "device_temperature"
            ):
                temperatures.append(entity.entity_id)
            elif (
                entity.domain == "binary_sensor"
                and entity.device_class in WINDOW_DEVICE_CLASSES
            ):
                windows.append(entity.entity_id)
        rooms.append(
            RoomDefinition(
                area_id=area_id,
                name=areas.get(area_id, area_id),
                valves=tuple(
                    sorted(valves_by_area[area_id], key=lambda valve: valve.device_id)
                ),
                temperature_entity_ids=tuple(sorted(temperatures)),
                window_entity_ids=tuple(sorted(windows)),
            )
        )
    return tuple(rooms)


async def async_discover_rooms(
    hass, selected_area_ids: set[str]
) -> tuple[RoomDefinition, ...]:
    """Read current HA registries and apply the pure discovery rules above."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    devices = tuple(
        DeviceRecord(
            device_id=device.id,
            manufacturer=device.manufacturer,
            model=device.model,
            area_id=device.area_id,
        )
        for device in device_registry.devices
    )
    entities = tuple(
        EntityRecord(
            entity_id=entity.entity_id,
            domain=entity.domain,
            platform=entity.platform,
            unique_id=entity.unique_id,
            device_id=entity.device_id,
            area_id=entity.area_id,
            device_class=entity.device_class or entity.original_device_class,
            disabled=entity.disabled,
            translation_key=entity.translation_key,
        )
        for entity in entity_registry.entities.values()
    )
    areas = {area.id: area.name for area in area_registry.areas.values()}
    return discover_rooms(
        devices=devices,
        entities=entities,
        areas=areas,
        selected_area_ids=selected_area_ids,
    )
