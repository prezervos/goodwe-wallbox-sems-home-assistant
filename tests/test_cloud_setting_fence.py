"""Compound settings use fresh readback under the real shared API lock."""
import asyncio
import importlib
import json
import threading
from unittest.mock import Mock

import pytest
import requests

from tests.test_sems_api import _make_api, sems_api_module as module

ModeError = importlib.import_module(module.__package__ + ".charge_mode_policy").ModeVerificationError


def reply(code="00000", data=True):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"code": code, "data": data}).encode()
    return response


@pytest.fixture
def cloud(monkeypatch):
    api = _make_api()
    api._ensure_plant_id = Mock(return_value="PLANT")
    api._ensure_web_token = Mock(return_value=True)
    api._build_web_headers = Mock(return_value={})
    state = {"sn": "TEST", "chargeMode": 0, "_reported_charge_mode": 0,
             "max_energy": 0, "charge_target_soc": 0, "set_charge_power": 11.0}
    api.get_data_gen2 = Mock(side_effect=lambda serial: dict(state))
    post = Mock(return_value=reply())
    monkeypatch.setattr(module.requests, "post", post)
    yield api, state, post
    api.close()


def test_companion_edit_waits_for_matching_readback_and_preserves_reported_power(cloud):
    api, state, post = cloud
    assert api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    with pytest.raises(ModeError, match="Previous cloud setting"):
        api.edit_mode_parameter("TEST", 0, "soc_target", 80)
    assert post.call_count == 1
    # Stale reads, including an ordinary coordinator update, do not authorize a write.
    assert api.get_data_gen2("TEST")["max_energy"] == 0
    state["max_energy"] = 10
    assert api.edit_mode_parameter("TEST", 0, "soc_target", 80)
    assert post.call_args.kwargs["json"]["maxEnergy"] == 10
    assert post.call_args.kwargs["json"]["soc"] == 80
    assert post.call_args.kwargs["json"]["chargeMaxPower"] == 11.0
    assert state["charge_target_soc"] == 0  # Intent never becomes telemetry.


def test_rejection_drops_only_current_candidate_and_keeps_confirmed_companions(cloud):
    api, state, post = cloud
    assert api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    state["max_energy"] = 10
    post.return_value = reply("A0201", False)
    assert not api.edit_mode_parameter("TEST", 0, "max_energy", 20)
    assert not api._pending_mode_edits
    post.return_value = reply()
    assert api.edit_mode_parameter("TEST", 0, "soc_target", 80)
    assert post.call_args.kwargs["json"]["maxEnergy"] == 10


@pytest.mark.parametrize("failure", ["R0305", "C0001", "malformed", "timeout", "disconnect"])
def test_uncertain_delivery_fences_following_compound_writes_but_not_stop(cloud, failure):
    api, state, post = cloud
    if failure in ("R0305", "C0001"):
        post.return_value = reply(failure, False)
    elif failure == "malformed":
        post.return_value = reply(data=[])
        post.return_value._content = b"not json"
    else:
        post.side_effect = requests.Timeout() if failure == "timeout" else requests.ConnectionError()
    with pytest.raises((module.CloudSettingError, TimeoutError)):
        api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    calls = post.call_count
    assert calls == 1  # An uncertain acknowledgement must never replay a write.
    post.side_effect = None
    post.return_value = reply()
    for write in (lambda: api.edit_mode_parameter("TEST", 0, "soc_target", 80),
                  lambda: api.set_charge_mode_gen2("TEST", 0, 5),
                  lambda: api.set_charge_mode_gen2("TEST", 1)):
        with pytest.raises(ModeError):
            write()
    assert post.call_count == calls
    assert api.change_status_gen2("TEST", "stop")
    assert post.call_count == calls + 1
    state["max_energy"] = 10
    assert api.set_charge_mode_gen2("TEST", 0, 5)
    assert api._pending_mode_edits["TEST"] == {"chargeMode": 0, "set_charge_power": 5}
    state["set_charge_power"] = 5
    api._confirm_previous_mode_edit("TEST")
    assert not api._pending_mode_edits


def test_partial_edit_token_renewal_reuses_payload_without_reentering_its_own_fence(cloud):
    api, _, post = cloud
    post.side_effect = [reply("C0602", False), reply()]
    assert api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    assert post.call_count == 2
    assert post.call_args_list[0].kwargs["json"] == post.call_args_list[1].kwargs["json"]
    assert api._pending_mode_edits["TEST"]["max_energy"] == 10


def test_pretransport_failure_does_not_leave_a_pending_write(cloud):
    api, _, post = cloud
    api._ensure_plant_id.return_value = None
    assert not api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    assert not api._pending_mode_edits
    post.assert_not_called()


async def test_competing_edits_cannot_read_companions_until_first_write_finishes(cloud):
    api, state, post = cloud
    entered, release = threading.Event(), threading.Event()
    second_started = threading.Event()
    def send(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
            state["max_energy"] = 10
        return reply()
    post.side_effect = send
    first = asyncio.create_task(asyncio.to_thread(api.edit_mode_parameter, "TEST", 0, "max_energy", 10))
    assert await asyncio.to_thread(entered.wait, 5)
    def second():
        second_started.set()
        return api.edit_mode_parameter("TEST", 0, "soc_target", 80)
    later = asyncio.create_task(asyncio.to_thread(second))
    try:
        assert await asyncio.to_thread(second_started.wait, 5)
        assert api.get_data_gen2.call_count == 1
        assert post.call_count == 1
    finally:
        release.set()
        assert await first
        assert await later
    assert post.call_args_list[1].kwargs["json"]["maxEnergy"] == 10


@pytest.mark.parametrize("invalid", [True, 1.0001, "NaN"])
def test_malformed_companion_never_clears_pending_fence(cloud, invalid):
    api, state, post = cloud
    assert api.edit_mode_parameter("TEST", 0, "max_energy", 1)
    state["max_energy"] = invalid
    with pytest.raises(ModeError):
        api.edit_mode_parameter("TEST", 0, "soc_target", 80)
    assert api._pending_mode_edits["TEST"]["max_energy"] == 1
    state["max_energy"] = 0
    with pytest.raises(ModeError):
        api.edit_mode_parameter("TEST", 0, "soc_target", 80)
    assert post.call_count == 1


@pytest.mark.parametrize("where", ["headers", "gate"])
async def test_expired_budget_before_transport_clears_candidate(cloud, monkeypatch, where):
    api, state, post = cloud
    budget_module = importlib.import_module(module.__package__ + ".operation_budget")
    budget = budget_module.OperationBudget(60)
    original = api._request_gate.request
    def expire(*args, **kwargs):
        budget.deadline = -1
        return {} if where == "headers" else original(*args, **kwargs)
    token = budget_module.CURRENT_BUDGET.set(budget)
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(api if where == "headers" else api._request_gate,
                           "_build_web_headers" if where == "headers" else "request", expire)
            with pytest.raises(TimeoutError):
                api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    finally:
        budget_module.CURRENT_BUDGET.reset(token)
    post.assert_not_called()
    assert not api._pending_mode_edits
    assert api.edit_mode_parameter("TEST", 0, "soc_target", 80)
    assert post.call_args.kwargs["json"]["maxEnergy"] == 0


def test_generation2_minimum_power_is_independent_of_pending_mode_edit(cloud):
    api, state, post = cloud
    state["ensure_minimum_charging_power"] = False
    assert api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    api.set_config_gen2 = Mock(return_value=True)
    assert api.set_minimum_power_checked("TEST", "2", True, "UTC")
    api.set_config_gen2.assert_called_once_with("TEST", ensureMinimumChargingPower=170)
    assert api._pending_mode_edits["TEST"]["max_energy"] == 10
    assert post.call_count == 1


@pytest.mark.parametrize("target,field,value", [(0, "set_charge_power", 5), (1, "chargeMode", 1)])
def test_explicit_mode_or_power_write_fences_later_partial_edit(cloud, target, field, value):
    api, state, post = cloud
    assert api.set_charge_mode_gen2("TEST", target, 5 if target == 0 else None)
    with pytest.raises(ModeError):
        api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    assert post.call_count == 1
    state[field] = value
    state["_reported_charge_mode"] = target
    if target == 1:
        state.update(min_energy=0, finish_time="0")
    assert api.edit_mode_parameter("TEST", target, "max_energy", 10)
    payload = post.call_args.kwargs["json"]
    assert payload["mode"] == target
    assert payload.get("chargeMaxPower") == (5 if target == 0 else None)


def test_mode_only_confirmation_does_not_require_unsupported_energy_targets(cloud):
    api, state, post = cloud
    state.pop("max_energy")
    state.pop("charge_target_soc")
    assert api.set_charge_mode_gen2("TEST", 1)
    state.update(chargeMode=1, _reported_charge_mode=1)
    assert api.set_charge_mode_gen2("TEST", 0, 5)
    assert post.call_count == 2


def test_uncertain_first_attempt_never_replays_or_renews_token(cloud):
    api, _, post = cloud
    post.side_effect = [reply("R0305", False), reply("C0602", False)]
    api._invalidate_rejected_session = Mock()
    with pytest.raises(module.CloudSettingError):
        api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    assert api._pending_mode_edits["TEST"]["max_energy"] == 10
    assert post.call_count == 1
    api._invalidate_rejected_session.assert_not_called()


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
async def test_rejected_token_attempt_closes_delivery_risk_before_interrupted_retry(cloud, interrupt):
    api, _, post = cloud
    budget_module = importlib.import_module(module.__package__ + ".operation_budget")
    budget = budget_module.OperationBudget(60)
    post.side_effect = [reply("C0602", False)]
    def interrupt_renewal():
        if interrupt == "cancel":
            budget.cancelled.set()
        else:
            budget.deadline = -1
    api._invalidate_rejected_session = interrupt_renewal
    token = budget_module.CURRENT_BUDGET.set(budget)
    try:
        expected = budget_module.BudgetCancelled if interrupt == "cancel" else TimeoutError
        with pytest.raises(expected):
            api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    finally:
        budget_module.CURRENT_BUDGET.reset(token)
    assert not api._pending_mode_edits
    assert post.call_count == 1


def test_token_retry_transport_timeout_retains_its_own_delivery_risk(cloud):
    api, _, post = cloud
    post.side_effect = [reply("C0602", False), requests.Timeout()]
    with pytest.raises(TimeoutError):
        api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    assert api._pending_mode_edits["TEST"]["max_energy"] == 10
    assert post.call_count == 2


def test_foreign_detail_cannot_confirm_pending_write_through_real_mapper(cloud):
    api, state, post = cloud
    assert api.edit_mode_parameter("TEST", 0, "max_energy", 10)
    from types import MethodType
    api.get_data_gen2 = MethodType(module.SemsApi.get_data_gen2, api)
    post.return_value = reply(data={"sn": "OTHER", "chargeMode": 0, "maxEnergy": 10,
                                   "soc": 0, "chargePowerSetted": 11})
    with pytest.raises(ModeError):
        api.set_charge_mode_gen2("TEST", 1)
    assert api._pending_mode_edits["TEST"]["max_energy"] == 10
    assert post.call_count == 2
    assert post.call_args.args[0].endswith("/detail")
