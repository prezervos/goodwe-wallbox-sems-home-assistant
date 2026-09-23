"""Plug and Charge encoding and missing-data regression checks."""

from unittest.mock import MagicMock, patch

import pytest

from tests.test_sems_api import _make_api
from tests.test_switch import SAMPLE_SN, _FakeCoordinator, _switch_mod


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        (0, False),
        (1, True),
        (170, True),
        ("0", False),
        ("1", True),
        ("170", True),
        (42, None),
        ("unknown", None),
    ],
)
def test_reported_plug_charge_mapping(raw, expected):
    api = _make_api()
    response = MagicMock()
    response.json.return_value = {
        "code": "00000",
        "data": {"chargedNow": raw, "sn": SAMPLE_SN},
    }
    with (
        patch.object(api, "_ensure_web_token", return_value=True),
        patch.object(api, "_ensure_plant_id", return_value="test"),
        patch.object(api, "_build_web_headers", return_value={}),
        patch("requests.post", return_value=response),
    ):
        assert api.get_data_gen2(SAMPLE_SN)["plug_and_charge"] is expected


@pytest.mark.parametrize("reported", [None, False, True])
def test_ack_and_pending_intent_do_not_replace_reported_state(reported):
    owner = _FakeCoordinator({SAMPLE_SN: {"plug_and_charge": reported}})
    entity = _switch_mod.SemsPlugAndChargeSwitch(owner, SAMPLE_SN, MagicMock())
    entity._pending_state = not reported
    assert entity.is_on is reported
    assert entity.unique_id == SAMPLE_SN + "-switch-plug-and-charge"


def test_current_sems_plus_payload_does_not_use_legacy_magic_value():
    entity = _switch_mod.SemsPlugAndChargeSwitch(
        _FakeCoordinator({SAMPLE_SN: {}}), SAMPLE_SN, MagicMock()
    )
    assert entity._set_config_on == {"chargedNow": 1}
    assert entity._set_config_off == {"chargedNow": 0}
    assert entity.is_on is None


@pytest.mark.parametrize("target", [False, True])
@pytest.mark.parametrize("accepted", [False, True, TimeoutError("uncertain write")])
async def test_service_write_preserves_readback_and_surfaces_rejection(target, accepted):
    from types import SimpleNamespace

    owner = _FakeCoordinator({SAMPLE_SN: {"plug_and_charge": not target}})
    owner.schedule_delayed_refresh = MagicMock()
    api = MagicMock()
    if isinstance(accepted, Exception):
        api.set_config_gen2.side_effect = accepted
    else:
        api.set_config_gen2.return_value = accepted
    entity = _switch_mod.SemsPlugAndChargeSwitch(owner, SAMPLE_SN, api)

    async def execute(function):
        return function()

    entity.hass = SimpleNamespace(async_add_executor_job=execute)
    command = entity.async_turn_on if target else entity.async_turn_off
    if accepted is True:
        await command()
    else:
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            await command()
    api.set_config_gen2.assert_called_once_with(SAMPLE_SN, chargedNow=int(target))
    owner.schedule_delayed_refresh.assert_called_once_with(5.0)
    assert entity.is_on is (not target)
    assert entity._pending_state is None
