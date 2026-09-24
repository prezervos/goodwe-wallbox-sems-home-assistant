"""Cloud current-limit regressions shared by both cloud entity paths."""

import pytest
from homeassistant.exceptions import HomeAssistantError
from tests.test_native_cloud_settings import owner, settings
from tests.test_number import _number_mod, _FakeCoordinator, _make_entity, SAMPLE_SN
from unittest.mock import Mock


@pytest.fixture(params=["cloud", "native_cloud"])
def current_entity(request, monkeypatch):
    """Return an entity, its telemetry mapping and the shared API mock."""
    if request.param == "cloud":
        reference = _make_entity()
        data = {"sn": SAMPLE_SN, "currentLimit": 16}
        coordinator = _FakeCoordinator({SAMPLE_SN: data})
        coordinator.schedule_delayed_refresh = Mock()
        entity = _number_mod.SemsCurrentLimitNumber(coordinator, SAMPLE_SN, reference.api)
        entity.hass = reference.hass
        entity.api.fetch_device_info.return_value = {"productModel": "MODEL"}
        entity.api.get_data_gen2.return_value = dict(data)
        entity.api.set_config_gen2.return_value = True
        return entity, data, entity.api, SAMPLE_SN
    monkeypatch.setattr(settings.NativeEntity, "available", property(lambda self: True), raising=False)
    instance = owner()
    instance.cloud_settings.valid = True
    instance.cloud_settings.values = dict(instance.cloud.get_data_gen2.return_value)
    entity = next(e for e in settings.setup_cloud_settings("number", instance)
                  if e.setting.field == "currentLimit")
    return entity, instance.cloud_settings.values, instance.cloud, "TEST"


@pytest.mark.parametrize("reported", [0, 16, 32, 63, 16.25, 2000])
def test_preserves_valid_observation_and_official_default_range(current_entity, reported):
    entity, data, api, serial = current_entity
    data["currentLimit"] = reported
    assert entity.native_value == reported
    assert (entity.native_min_value, entity.native_max_value) == (0, 2000)
    assert entity.available
    assert entity.extra_state_attributes["write_supported"] is True
    api.set_config_gen2.assert_not_called()


def metadata(minimum=0, maximum=100):
    return {"charge_pile_dynamic_load_import_current_limit": {"min": minimum, "max": maximum}}


@pytest.mark.parametrize("ranges,expected", [
    (None, (0, 2000)), ({}, (0, 2000)),
    (metadata(6, 80), (6, 80)), (metadata(None, 80), (0, 80)),
    (metadata(6, None), (6, 2000)), (metadata(63, 63), (63, 63)),
    (metadata("0", "100"), (0, 100)),
])
def test_device_metadata_controls_ui(current_entity, ranges, expected):
    entity, data, api, serial = current_entity
    data.update(currentLimit=63, controlItemRanges=ranges)
    assert (entity.native_min_value, entity.native_max_value) == expected
    assert entity.available


@pytest.mark.parametrize("ranges", [False, [], metadata(80, 6), metadata(True, 80),
    metadata(-1, 80), metadata(0, "nan"), metadata(0, "inf"),
    {"charge_pile_dynamic_load_import_current_limit": []}])
@pytest.mark.asyncio
async def test_malformed_metadata_disables_control_and_never_writes(current_entity, ranges):
    entity, data, api, serial = current_entity
    data.update(currentLimit=63, controlItemRanges=ranges)
    assert not entity.available
    assert entity.extra_state_attributes["reported_current_limit"] == 63
    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_set_native_value(16)
    assert caught.value.translation_key == "current_limit_range_unverified"
    api.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
async def test_contradictory_report_not_clamped_or_authorized(current_entity):
    entity, data, api, serial = current_entity
    data.update(currentLimit=63, controlItemRanges=metadata(0, 32))
    assert not entity.available
    assert entity.extra_state_attributes["reported_current_limit"] == 63
    assert (entity.native_min_value, entity.native_max_value) == (0, 32)
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(32)
    api.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [2001, -1, 12.345, True, float("nan"), float("inf")])
async def test_invalid_request_never_reaches_api(current_entity, requested):
    entity, data, api, serial = current_entity
    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_set_native_value(requested)
    assert caught.value.translation_key == "current_limit_invalid"
    api.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_metadata_prevents_stale_range_write(current_entity):
    entity, data, api, serial = current_entity
    api.fetch_device_info.return_value = {"controlItemRanges": metadata(0, 32)}
    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_set_native_value(63)
    assert caught.value.translation_key == "current_limit_invalid"
    api.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [0, 32, 63, 63.25, 2000])
async def test_supported_write_preserves_report_until_confirmation(current_entity, requested):
    entity, data, api, serial = current_entity
    await entity.async_set_native_value(requested)
    api.set_config_gen2.assert_called_once_with(serial, currentLimit=requested)
    assert entity.native_value == 16


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {}, {"sn": "OTHER", "controlItemRanges": {}}])
async def test_failed_or_wrong_device_metadata_cannot_authorize_write(current_entity, bad):
    entity, data, api, serial = current_entity
    api.fetch_device_info.return_value = bad
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(12)
    api.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [None, True, "nan", "inf", -1])
async def test_missing_or_invalid_report_is_never_fabricated(current_entity, reported):
    entity, data, api, serial = current_entity
    data["currentLimit"] = reported
    assert entity.native_value is None
    assert not entity.available
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(12)
    api.set_config_gen2.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_device_read_cannot_authorize_write(current_entity):
    entity, data, api, serial = current_entity
    api.get_data_gen2.return_value["sn"] = "OTHER"
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(12)
    api.set_config_gen2.assert_not_called()


def test_new_consistent_metadata_recovers_entity(current_entity):
    entity, data, api, serial = current_entity
    data.update(currentLimit=63, controlItemRanges=metadata(0, 32))
    assert not entity.available
    data["controlItemRanges"] = metadata(0, 100)
    assert entity.available
    assert entity.native_value == 63


def test_real_api_propagates_metadata_without_extra_telemetry_requests():
    """Discovery is cached per serial; polling does not fetch metadata repeatedly."""
    from unittest.mock import MagicMock, patch
    from tests.test_sems_api import SemsApi
    api = SemsApi(MagicMock(), "fixture", "fixture")
    api._web_token = {"uid": "fixture"}
    api._plant_id = "fixture"
    info = MagicMock(status_code=200)
    info.json.return_value = {"code": "00000", "data": {"controlItemRanges": metadata(6, 80)}}
    detail = MagicMock(status_code=200)
    detail.json.return_value = {"code": "00000", "data": {"sn": "ONE", "currentLimit": 63}}
    with patch("requests.get", return_value=info) as get, patch("requests.post", return_value=detail) as post:
        api.fetch_device_info("ONE")
        for _ in range(2):
            assert api.get_data_gen2("ONE")["controlItemRanges"] == metadata(6, 80)
        get.assert_called_once()
        assert post.call_count == 2
        assert "TWO" not in api._control_item_ranges
        info.json.return_value["data"] = {"productModel": "MODEL"}
        api.fetch_device_info("ONE")
        assert api.get_data_gen2("ONE")["controlItemRanges"] is None
        info.json.return_value["data"] = {"sn": "OTHER", "controlItemRanges": metadata(0, 3000)}
        assert api.fetch_device_info("ONE") == {}
        assert api._control_item_ranges["ONE"] is None


@pytest.mark.asyncio
async def test_failed_ack_does_not_publish_requested_current(current_entity):
    entity, data, api, serial = current_entity
    api.set_config_gen2.return_value = False
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(63.25)
    api.set_config_gen2.assert_called_once_with(serial, currentLimit=63.25)
    assert entity.native_value == 16
