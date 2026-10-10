"""Explicit calibration action routes only to its selected Danfoss climate entity."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.danfoss_ally_coordinator import async_setup
from custom_components.danfoss_ally_coordinator.const import DOMAIN


class ServiceRegistry:
    def __init__(self):
        self.handlers = {}

    def async_register(self, domain, service, handler, *, schema, **kwargs):
        self.handlers[(domain, service)] = handler


def test_calibration_action_is_explicit_and_scoped():
    hass = SimpleNamespace(data={}, services=ServiceRegistry())
    coordinator = SimpleNamespace(
        rooms=(
            SimpleNamespace(
                valves=(SimpleNamespace(climate_entity_id="climate.office"),)
            ),
        ),
        async_calibrate_local_sensor=AsyncMock(return_value={"offset": 0.4}),
    )
    asyncio.run(async_setup(hass, {}))
    hass.data[DOMAIN]["entry"] = coordinator
    action = hass.services.handlers[(DOMAIN, "calibrate_local_sensor")]
    with pytest.raises(HomeAssistantError):
        asyncio.run(action(SimpleNamespace(data={"entity_id": "climate.other"})))
    coordinator.async_calibrate_local_sensor.assert_not_awaited()
    response = asyncio.run(
        action(SimpleNamespace(data={"entity_id": "climate.office"}))
    )
    assert response == {"offset": 0.4}
    coordinator.async_calibrate_local_sensor.assert_awaited_once()
    assert coordinator.async_calibrate_local_sensor.await_args.args == (
        "climate.office",
    )
