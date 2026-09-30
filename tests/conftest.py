"""Provide lightweight Home Assistant contracts for offline unit tests.

Production modules are loaded from custom_components/sems_wallbox under isolated
package names. Actual HA lifecycle, registry and service behavior is covered by
scripts/ha_*_smoke.py in separate processes with the real HA dependency.
"""

import sys
import types

import pytest
import requests


def _register(name: str) -> types.ModuleType:
    """Return or create a stub module in sys.modules."""
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


# --------------------------------------------------------------------------
# homeassistant top-level package
# --------------------------------------------------------------------------
_register("homeassistant")

# --------------------------------------------------------------------------
# homeassistant.exceptions
# --------------------------------------------------------------------------
exc_mod = _register("homeassistant.exceptions")
if not hasattr(exc_mod, "HomeAssistantError"):
    class HomeAssistantError(Exception):
        def __init__(self, *args, translation_domain=None, translation_key=None,
                     translation_placeholders=None):
            super().__init__(*args)
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders

    exc_mod.HomeAssistantError = HomeAssistantError
    exc_mod.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (HomeAssistantError,), {})

# --------------------------------------------------------------------------
# homeassistant.const
# --------------------------------------------------------------------------
const_mod = _register("homeassistant.const")
if not hasattr(const_mod, "CONF_PASSWORD"):
    const_mod.CONF_PASSWORD = "password"
    const_mod.CONF_USERNAME = "username"
    const_mod.CONF_SCAN_INTERVAL = "scan_interval"
    const_mod.CONF_URL = "url"
    const_mod.CONF_HOST = "host"
    const_mod.CONF_PORT = "port"

    class Platform:
        BINARY_SENSOR = "binary_sensor"
        NUMBER = "number"
        SELECT = "select"
        SENSOR = "sensor"
        SWITCH = "switch"

    class UnitOfPower:
        KILO_WATT = "kW"

    class UnitOfEnergy:
        KILO_WATT_HOUR = "kWh"

    class UnitOfElectricCurrent:
        AMPERE = "A"

    class UnitOfElectricPotential:
        VOLT = "V"

    class UnitOfTime:
        MINUTES = "min"

    const_mod.UnitOfElectricCurrent = UnitOfElectricCurrent
    const_mod.UnitOfElectricPotential = UnitOfElectricPotential

    const_mod.Platform = Platform
    const_mod.UnitOfPower = UnitOfPower
    const_mod.UnitOfEnergy = UnitOfEnergy
    const_mod.UnitOfTime = UnitOfTime

    class EntityCategory:
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    const_mod.EntityCategory = EntityCategory

# --------------------------------------------------------------------------
# homeassistant.core
# --------------------------------------------------------------------------
core_mod = _register("homeassistant.core")
if not hasattr(core_mod, "HomeAssistant"):
    core_mod.HomeAssistant = object
    core_mod.callback = lambda f: f

# --------------------------------------------------------------------------
# homeassistant.config_entries
# --------------------------------------------------------------------------
ce_mod = _register("homeassistant.config_entries")
if not hasattr(ce_mod, "ConfigEntry"):
    class ConfigEntry:
        entry_id = "test_entry"
        def __init__(self):
            self.options = {}
            self.data = {}
    ce_mod.ConfigEntry = ConfigEntry
    ce_mod.ConfigEntryNotReady = type("ConfigEntryNotReady", (exc_mod.HomeAssistantError,), {})

# --------------------------------------------------------------------------
# homeassistant.components.*
# --------------------------------------------------------------------------
_register("homeassistant.components")

sensor_mod = _register("homeassistant.components.sensor")
if not hasattr(sensor_mod, "SensorDeviceClass"):
    class SensorDeviceClass:
        ENUM = "enum"
        POWER = "power"
        ENERGY = "energy"
        CURRENT = "current"
        VOLTAGE = "voltage"
    class SensorStateClass:
        TOTAL_INCREASING = "total_increasing"
        MEASUREMENT = "measurement"
    class SensorEntity:
        pass
    sensor_mod.SensorDeviceClass = SensorDeviceClass
    sensor_mod.SensorStateClass = SensorStateClass
    sensor_mod.SensorEntity = SensorEntity

switch_mod = _register("homeassistant.components.switch")
if not hasattr(switch_mod, "SwitchDeviceClass"):
    class SwitchDeviceClass:
        SWITCH = "switch"
    class SwitchEntity:
        pass
    switch_mod.SwitchDeviceClass = SwitchDeviceClass
    switch_mod.SwitchEntity = SwitchEntity

select_mod = _register("homeassistant.components.select")
if not hasattr(select_mod, "SelectEntity"):
    class SelectEntity:
        pass
    class SelectEntityDescription:
        def __init__(self, key=None, entity_category=None, translation_key=None, **kwargs):
            self.key = key
            self.entity_category = entity_category
            self.translation_key = translation_key
    select_mod.SelectEntity = SelectEntity
    select_mod.SelectEntityDescription = SelectEntityDescription

number_mod = _register("homeassistant.components.number")
if not hasattr(number_mod, "NumberDeviceClass"):
    class NumberDeviceClass:
        POWER = "power"
        ENERGY = "energy"
        CURRENT = "current"
    class NumberEntity:
        pass
    class NumberEntityDescription:
        pass
    number_mod.NumberDeviceClass = NumberDeviceClass
    number_mod.NumberEntity = NumberEntity
    number_mod.NumberEntityDescription = NumberEntityDescription
    number_mod.NumberMode = type("NumberMode", (), {"BOX": "box"})

# --------------------------------------------------------------------------
# homeassistant.helpers.*
# --------------------------------------------------------------------------
_register("homeassistant.helpers")

coord_mod = _register("homeassistant.helpers.update_coordinator")
if not hasattr(coord_mod, "CoordinatorEntity"):
    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator
        async def async_added_to_hass(self):
            pass
    class DataUpdateCoordinator:
        pass
    class UpdateFailed(Exception):
        pass
    coord_mod.CoordinatorEntity = CoordinatorEntity
    coord_mod.DataUpdateCoordinator = DataUpdateCoordinator
    coord_mod.UpdateFailed = UpdateFailed

ep_mod = _register("homeassistant.helpers.entity_platform")
if not hasattr(ep_mod, "AddEntitiesCallback"):
    ep_mod.AddEntitiesCallback = object

# Minimal selector validation; actual serialization/translations use real HA smoke tests.
selector_mod = _register("homeassistant.helpers.selector")
if not hasattr(selector_mod, "SelectSelector"):
    class SelectSelector:
        def __init__(self, config):
            self.config = config

        def __call__(self, value):
            import voluptuous as vol
            if not isinstance(value, str) or value not in self.config["options"]:
                raise vol.Invalid("Invalid selector option")
            return value

    selector_mod.SelectSelector = SelectSelector
    selector_mod.SelectSelectorConfig = dict

binary_mod = _register("homeassistant.components.binary_sensor")
if not hasattr(binary_mod, "BinarySensorEntity"):
    binary_mod.BinarySensorEntity = type("BinarySensorEntity", (), {})

if not hasattr(selector_mod, "NumberSelector"):
    class NumberSelector:
        def __init__(self, config):
            self.config = config

        def __call__(self, value):
            import voluptuous as vol
            return vol.All(vol.Coerce(float), vol.Range(
                min=self.config.get("min"), max=self.config.get("max")
            ))(value)

    selector_mod.NumberSelector = NumberSelector
    selector_mod.NumberSelectorConfig = dict
    selector_mod.NumberSelectorMode = types.SimpleNamespace(BOX="box")


# A missing API mock must fail locally rather than contact a real SEMS endpoint.


@pytest.fixture(autouse=True)
def forbid_unmocked_http(monkeypatch):
    """Keep unit tests offline; protocol tests use their own loopback sockets."""
    def reject_request(*args, **kwargs):
        pytest.fail("Unexpected HTTP request: install an explicit response fixture")

    monkeypatch.setattr(requests.sessions.Session, "request", reject_request)
