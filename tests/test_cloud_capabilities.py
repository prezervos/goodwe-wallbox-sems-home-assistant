"""Exercise generation gates across both cloud entity implementations."""

import importlib
from types import SimpleNamespace

import pytest

from tests import test_number as legacy
from tests.test_native_cloud_settings import owner, settings
from tests.test_native_transport import PACKAGE

capabilities = importlib.import_module(PACKAGE + ".cloud_capabilities")
entities = importlib.import_module(PACKAGE + ".native_entities")


@pytest.mark.asyncio
@pytest.mark.parametrize("generation,controls,expected", [
    ("1", ["Dynamic_Load_Control"], {"currentLimit"}),
    ("2", ["Dynamic_Load_Control", "Output_Power_Setting"],
     {"currentLimit", "rated_max_charge_power", "max_energy", "min_energy", "charge_target_soc"}),
    ("2", ["Output_Power_Setting"],
     {"rated_max_charge_power", "max_energy", "min_energy", "charge_target_soc"}),
    ("", ["Dynamic_Load_Control"],
     {"currentLimit", "max_energy", "min_energy", "charge_target_soc"}),
    ("1", ["Output_Power_Setting"], {"rated_max_charge_power"}),
])
async def test_known_capabilities_match_both_cloud_number_paths(
        monkeypatch, generation, controls, expected):
    """Compare actual setup paths, not only the shared predicate."""
    caps = {"pile_generation": generation, "more_device_controls": controls}
    names, removed = await legacy._setup_cloud_numbers(monkeypatch, caps)
    fields = {
        "SemsCurrentLimitNumber": "currentLimit",
        "SemsOutputPowerLimitNumber": "rated_max_charge_power",
        "SemsMaxEnergyNumber": "max_energy",
        "SemsMinEnergyNumber": "min_energy",
        "SemsTargetSocNumber": "charge_target_soc",
    }
    assert {fields[name] for name in names if name in fields} == expected
    instance = owner()
    instance.entry.data = caps
    native = settings.setup_cloud_settings("number", instance)
    assert {entity.setting.field for entity in native} == expected
    assert {unique_id for _, unique_id in removed} == set(
        capabilities.unsupported_ids(caps, "number", legacy.SAMPLE_SN))
    if generation == "1":
        assert "SemsNumber" in names
        assert settings.setup_cloud_settings("select", instance) == []
    else:
        assert [e.setting.field for e in settings.setup_cloud_settings("select", instance)] == ["finish_time"]


@pytest.mark.asyncio
@pytest.mark.parametrize("controls", [None, [], "Dynamic_Load_Control", [None], [""]])
async def test_unknown_capabilities_preserve_each_path_and_skip_cleanup(monkeypatch, controls):
    caps = {"more_device_controls": controls}
    names, removed = await legacy._setup_cloud_numbers(monkeypatch, caps)
    assert names == {"SemsNumber", "SemsCurrentLimitNumber", "SemsMaxEnergyNumber",
                     "SemsMinEnergyNumber", "SemsTargetSocNumber"}
    assert removed == []
    instance = owner()
    instance.entry.data = caps
    assert {e.setting.field for e in settings.setup_cloud_settings("number", instance)} == {
        "currentLimit", "rated_max_charge_power", "max_energy", "min_energy", "charge_target_soc"}
    assert capabilities.unsupported_ids(caps, "number", "TEST") == []
    assert capabilities.unsupported_ids(caps, "select", "TEST") == []


def test_known_generation_is_independent_of_missing_extra_capabilities():
    caps = {"pile_generation": 1, "more_device_controls": []}
    assert capabilities.control_support(caps, "set_charge_power") is True
    assert capabilities.control_support(caps, "currentLimit") is None
    assert capabilities.control_support(caps, "rated_max_charge_power") is None
    assert set(capabilities.unsupported_ids(caps, "number", "TEST")) == {
        "TEST-number-max-energy", "TEST-number-min-energy", "TEST-number-target-soc"}


def test_native_primary_controls_and_verified_local_switches_survive(monkeypatch):
    instance = owner()
    instance.serial = "5011KHCA234W0000"
    instance.entry.data = {"pile_generation": "1", "more_device_controls": ["Dynamic_Load_Control"]}
    instance.transport = SimpleNamespace(
        available=True, latest=SimpleNamespace(minimum_power=True))
    removed = []
    monkeypatch.setattr(capabilities, "remove_unsupported",
                        lambda hass, entry, platform, ids: removed.extend(ids))
    added = []
    entities.setup_platform("number", instance, added.extend)
    assert any(entity._attr_unique_id.endswith("_number_set_charge_power") for entity in added)
    assert "5011KHCA234W0000-number-output-power-limit" in removed
    switches = settings.setup_cloud_settings("switch", instance)
    assert any(isinstance(entity, settings.AutoStartSwitch) for entity in switches)
    assert any(isinstance(entity, settings.MinimumPowerSwitch) for entity in switches)
    instance.cloud = None
    assert settings.setup_cloud_settings("number", instance) == []
    switches = settings.setup_cloud_settings("switch", instance)
    assert {type(entity).__name__ for entity in switches} == {"AutoStartSwitch", "MinimumPowerSwitch"}
