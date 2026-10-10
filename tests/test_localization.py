"""Validate shipped English defaults and both UI translation catalogs."""

import json
from pathlib import Path
from types import SimpleNamespace

from custom_components.danfoss_ally_coordinator.config_flow import _area_option_label

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = ROOT / "custom_components" / "danfoss_ally_coordinator"


def _keys(value, prefix=""):
    """Collect translation leaf paths without requiring matching wording."""
    if isinstance(value, dict):
        return {
            key
            for name, child in value.items()
            for key in _keys(child, f"{prefix}.{name}" if prefix else name)
        }
    return {prefix}


def test_english_defaults_and_complete_german_translation():
    """Untranslated English installations and German installations have all labels."""
    strings = json.loads((DOMAIN / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (DOMAIN / "translations" / "en.json").read_text(encoding="utf-8")
    )
    german = json.loads(
        (DOMAIN / "translations" / "de.json").read_text(encoding="utf-8")
    )
    assert strings == english
    assert _keys(strings) == _keys(german)
    assert strings["config"]["step"]["user"]["title"] == "Select Danfoss Ally rooms"
    assert german["config"]["step"]["user"]["title"] == "Danfoss-Ally-Räume auswählen"
    assert (
        strings["services"]["refresh"]["name"]
        == "Refresh discovery and synchronization"
    )
    assert (
        german["services"]["refresh"]["name"]
        == "Erkennung und Synchronisierung aktualisieren"
    )


def test_area_label_uses_language_neutral_counts():
    """Dynamic area labels do not embed German or English-only prose."""
    room = SimpleNamespace(
        name="Office",
        valves=(object(), object()),
        temperature_entity_ids=("sensor.a", "sensor.b"),
        window_entity_ids=("binary_sensor.a",),
    )
    assert _area_option_label(room) == "Office (TRV 2 · T 2 · W 1)"


def test_english_project_documentation_and_service_fallback():
    """English is the default for repository docs and HA service descriptions."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    services = (DOMAIN / "services.yaml").read_text(encoding="utf-8")
    assert "## Installation" in readme
    assert "## Safety" in readme
    assert "Refresh discovery and synchronization" in services
