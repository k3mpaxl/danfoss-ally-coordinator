"""Store entity references by registry ID so renaming an entity ID keeps them."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


def to_registry_ids(hass: HomeAssistant, references: Iterable[str]) -> list[str]:
    """Turn entity IDs (or registry IDs) into stable registry IDs.

    Unknown references are kept unchanged, so nothing is lost silently.
    """
    registry = er.async_get(hass)
    return sorted(
        entry.id if (entry := registry.async_get(reference)) else reference
        for reference in references
    )


def to_entity_ids(hass: HomeAssistant, references: Iterable[str]) -> frozenset[str]:
    """Resolve stored registry IDs (or legacy entity IDs) to current entity IDs."""
    registry = er.async_get(hass)
    return frozenset(
        entry.entity_id if (entry := registry.async_get(reference)) else reference
        for reference in references
    )


def registry_id(hass: HomeAssistant, entity_id: str) -> str:
    """Return the registry ID of an entity, or the entity ID if it is unregistered."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry.id if entry else entity_id
