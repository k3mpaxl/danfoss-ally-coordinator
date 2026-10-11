"""Pure registry-discovery rules for Danfoss Ally room coordinators."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

DANFOSS_MANUFACTURER = "Danfoss"
DANFOSS_MODEL = "eTRV0103"
ZHA_PLATFORM = "zha"
TEMPERATURE_UNIQUE_SUFFIX = "-external_measured_room_sensor"
WINDOW_UNIQUE_SUFFIX = "-external_open_window_detected"

WINDOW_DETECTION_SUFFIX = "-open_window_detection"
SETPOINT_SOURCE_SUFFIX = "-setpoint_change_source"
CALIBRATION_SUFFIX = "-local_temperature_calibration"
COVERED_SUFFIX = "-radiator_covered"
LOAD_ESTIMATE_SUFFIX = "-load_estimate"
LOAD_ROOM_MEAN_SUFFIX = "-load_room_mean"
LOAD_BALANCING_ENABLE_SUFFIX = "-load_balancing_enable"
ADAPTATION_STATUS_SUFFIX = "-adaptation_run_status"
ERROR_CODE_SUFFIX = "-sw_error_code"
HEAT_AVAILABLE_SUFFIX = "-heat_available"
# Entities ZHA's built-in Danfoss quirk must expose before a valve is controlled.
REQUIRED_ENTITIES = (
    ("number", TEMPERATURE_UNIQUE_SUFFIX),
    ("switch", WINDOW_UNIQUE_SUFFIX),
)
WINDOW_DEVICE_CLASSES = frozenset({"window", "opening", "door"})


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """The registry properties needed to classify one HA device."""

    device_id: str
    manufacturer: str | None
    model: str | None
    area_id: str | None
    name: str | None = None


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
    # Read-only: the valve's covered-radiator mode (0x4016) selects the write cadence.
    covered_entity_id: str | None = None
    climate_entity_id: str | None = None
    window_detection_entity_id: str | None = None
    setpoint_source_entity_id: str | None = None
    calibration_entity_id: str | None = None
    name: str | None = None
    load_estimate_entity_id: str | None = None
    load_room_mean_entity_id: str | None = None
    load_balancing_enable_entity_id: str | None = None
    adaptation_status_entity_id: str | None = None
    error_entity_id: str | None = None
    # Catalogue §2.7: whether the heating system supplies heat (0x4030).
    heat_available_entity_id: str | None = None

    @property
    def supports_load_balancing(self) -> bool:
        """Whether ZHA exposes everything needed for catalogue §2.2."""
        return bool(
            self.load_estimate_entity_id
            and self.load_room_mean_entity_id
            and self.load_balancing_enable_entity_id
        )


@dataclass(frozen=True, slots=True)
class IncompleteValve:
    """A Danfoss valve on ZHA whose control entities are missing or disabled.

    It is shown and reported, but never written, until ZHA exposes them.
    """

    device_id: str
    name: str | None
    missing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoomDefinition:
    """All discovered eTRV targets and source entities for one HA area."""

    area_id: str
    name: str
    valves: tuple[ValveTarget, ...]
    temperature_entity_ids: tuple[str, ...]
    window_entity_ids: tuple[str, ...]
    incomplete_valves: tuple[IncompleteValve, ...] = ()


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
    device: DeviceRecord, entities: tuple[EntityRecord, ...]
) -> ValveTarget | IncompleteValve:
    """Find the typed ZHA control entities without depending on translated names."""
    all_device_entities = [
        entity
        for entity in entities
        if entity.device_id == device.device_id and entity.platform == ZHA_PLATFORM
    ]
    device_entities = [entity for entity in all_device_entities if not entity.disabled]
    missing = tuple(
        suffix.removeprefix("-")
        + (
            " (disabled)"
            if any(
                entity.domain == domain and entity.unique_id.endswith(suffix)
                for entity in all_device_entities
            )
            else ""
        )
        for domain, suffix in REQUIRED_ENTITIES
        if not any(
            entity.domain == domain and entity.unique_id.endswith(suffix)
            for entity in device_entities
        )
    )
    if missing:
        return IncompleteValve(device.device_id, device.name, missing)

    def optional(domain: str, suffix: str) -> str | None:
        return next(
            (
                entity.entity_id
                for entity in device_entities
                if entity.domain == domain and entity.unique_id.endswith(suffix)
            ),
            None,
        )

    climate = next(
        (entity.entity_id for entity in device_entities if entity.domain == "climate"),
        None,
    )
    return ValveTarget(
        device.device_id,
        optional("number", TEMPERATURE_UNIQUE_SUFFIX),
        optional("switch", WINDOW_UNIQUE_SUFFIX),
        optional("switch", COVERED_SUFFIX),
        climate,
        optional("sensor", WINDOW_DETECTION_SUFFIX),
        optional("sensor", SETPOINT_SOURCE_SUFFIX),
        optional("number", CALIBRATION_SUFFIX),
        device.name,
        optional("sensor", LOAD_ESTIMATE_SUFFIX),
        optional("number", LOAD_ROOM_MEAN_SUFFIX),
        optional("switch", LOAD_BALANCING_ENABLE_SUFFIX),
        optional("sensor", ADAPTATION_STATUS_SUFFIX),
        optional("sensor", ERROR_CODE_SUFFIX),
        optional("switch", HEAT_AVAILABLE_SUFFIX),
    )


def discover_rooms(
    *,
    devices: Iterable[DeviceRecord],
    entities: Iterable[EntityRecord],
    areas: dict[str, str],
    selected_area_ids: set[str],
) -> tuple[RoomDefinition, ...]:
    """Discover ZHA Danfoss room groups and all safe area-scoped source candidates.

    An area is a configuration hint, not proof of a physical room. The caller asks
    the user to select the discovered areas before writes can be enabled; the
    coordinator lets the user switch individual candidates off.
    """
    device_map = {device.device_id: device for device in devices}
    all_entities = tuple(entities)
    targets = _zha_danfoss_target_ids(device_map, all_entities)
    valves_by_area: dict[str, list[ValveTarget]] = {}
    incomplete_by_area: dict[str, list[IncompleteValve]] = {}
    for target_id in targets:
        device = device_map[target_id]
        area_id = device.area_id
        if area_id is None or area_id not in selected_area_ids:
            continue
        valve = _find_target(device, all_entities)
        if isinstance(valve, IncompleteValve):
            incomplete_by_area.setdefault(area_id, []).append(valve)
        else:
            valves_by_area.setdefault(area_id, []).append(valve)

    rooms: list[RoomDefinition] = []
    for area_id in sorted(valves_by_area.keys() | incomplete_by_area.keys()):
        area_targets = frozenset(
            valve.device_id
            for valve in (
                *valves_by_area.get(area_id, ()),
                *incomplete_by_area.get(area_id, ()),
            )
        )
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
                    sorted(
                        valves_by_area.get(area_id, ()),
                        key=lambda valve: valve.device_id,
                    )
                ),
                temperature_entity_ids=tuple(sorted(temperatures)),
                window_entity_ids=tuple(sorted(windows)),
                incomplete_valves=tuple(
                    sorted(
                        incomplete_by_area.get(area_id, ()),
                        key=lambda valve: valve.device_id,
                    )
                ),
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
            name=device.name_by_user or device.name,
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
