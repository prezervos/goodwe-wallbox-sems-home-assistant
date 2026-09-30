"""Home Assistant routing between SEMS cloud and owned native Socket A."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .write_confirmation import WriteConfirmation, confirmation_read, readback_debouncer
from .charge_mode_adapter import ModeTransportAdapter
from .charge_mode_policy import ChargeModePolicy, ModeVerificationError
from .cloud_observation import CloudAuthenticationError
from .native_adapter import NativeModeAdapter
from .native_connection_intent import ConnectionIntent
from .native_cloud_settings import CloudSettings
from .native_endpoint import EndpointManager
from .native_energy_polling import NativeEnergyPolling
from .native_configuration_polling import NativeConfigurationPolling
from .native_fallback import AutomaticFallback, report_age
from .native_intent import LatestIntent
from .native_control_fallback import ControlFallback
from .native_preflight import async_cloud_preflight
from .native_transport import NativeTransport

_LOGGER = logging.getLogger(__name__)


def native_data(state, observed_at):
    """Map only independently decoded native fields; do not invent cloud history."""
    return {
        "sn": state.serial,
        "name": f"GoodWe Wallbox {state.serial}",
        "status": "charging"
        if state.charging
        else ("standby" if state.stopped else "unknown"),
        "chargeMode": state.mode,
        "power": state.power_kw,
        "set_charge_power": state.limit_kw,
        "session_seconds": state.session_seconds,
        "session_energy_kwh": state.session_energy_kwh,
        "currents_a": state.currents_a,
        "voltages_v": state.voltages_v,
        "fault_code": state.fault_code,
        "connection": state.connection,
        "raw_state": state.state,
        "transport": "tcp",
        "observed_at": observed_at,
    }


class RoutingAdapter:
    """Route policy operations without mixing cloud and TCP in one transaction."""

    def __init__(self, coordinator):
        self.coordinator = coordinator

    def _adapter(self):
        owner = self.coordinator
        if getattr(owner, "transitioning", False) or (
            not owner.local and getattr(owner, "cloud_restored_at", None) is not None
        ):
            raise ModeVerificationError("Transport has not been verified after handover")
        if owner.local:
            return owner.native_adapter
        if owner.cloud_adapter is None:
            raise ModeVerificationError(
                "No cloud account configured; enable local TCP first"
            )
        return owner.cloud_adapter

    @property
    def preserves_power_all_modes(self):
        """Expose the currently validated transport capability to policy."""
        return self.coordinator.local

    async def read(self):
        return await self._adapter().read()

    async def write_mode(self, mode, before):
        return await self._adapter().write_mode(mode, before)

    async def start(self):
        owner = self.coordinator
        if owner.local:
            policy = owner.charge_mode_policy
            version = policy._version
            return await owner.native_adapter.start(
                power=policy.desired_power,
                start_allowed=lambda: not policy._closed and policy._version == version,
            )
        return await self._adapter().start()

    async def stop(self):
        return await self._adapter().stop()


class NativeCoordinator(DataUpdateCoordinator):
    """Keep stable entity identities while changing the active transport."""

    def __init__(self, hass, entry, cloud=None):
        super().__init__(
            hass,
            _LOGGER,
            name="GoodWe cloud/TCP",
            config_entry=entry,
            request_refresh_debouncer=readback_debouncer(hass, _LOGGER),
            update_interval=timedelta(seconds=5),
        )
        self.write_confirmation = WriteConfirmation(self)
        self.entry = entry
        self.serial = entry.data["wallbox_serial_No"]
        config = {**entry.data, **entry.options}
        self.peer = config["native_host"]
        self.port = config["native_port"]
        self.cloud = cloud
        self.cloud_settings = CloudSettings(self)
        self.connection_intent = ConnectionIntent(
            Store(hass, 1, f"sems_wallbox.{entry.entry_id}.connection_intent")
        )
        self.local = False
        self.transitioning = False
        self.transition_target = None
        self._closed = False
        self.cloud_restored_at = None
        self._handover_lock = asyncio.Lock()
        self._handover_store = Store(
            hass, 1, f"sems_wallbox.{entry.entry_id}.cloud_handover"
        )
        self.routing_epoch = 0
        self.transport = NativeTransport(
            self.serial, config.get("native_ingress_peer") or self.peer
        )
        self.native_adapter = NativeModeAdapter(self.transport)
        self.cloud_adapter = (
            ModeTransportAdapter(hass, self.serial, cloud) if cloud else None
        )
        self.endpoint = EndpointManager(
            self.peer,
            self.serial,
            config["native_advertised_host"],
            self.port,
            Store(hass, 1, f"sems_wallbox.{entry.entry_id}.native_endpoint"),
        )
        self.charge_mode_policy = ChargeModePolicy(
            RoutingAdapter(self),
            Store(hass, 1, f"sems_wallbox.{entry.entry_id}.charge_mode"),
            enabled=True,
            remember_mode=config.get("remember_charge_mode", False),
        )
        self.automatic_fallback = AutomaticFallback(
            self, config.get("native_auto_fallback", False)
        )
        self.pending_intent = LatestIntent(self)
        self.control_fallback = ControlFallback(self)
        self.energy_polling = NativeEnergyPolling(self)
        self.configuration_polling = NativeConfigurationPolling(self)
        from .native_power_limits import power_bounds

        self.initial_power = power_bounds(self.serial)[0]
        self.data = {
            self.serial: {"sn": self.serial, "transport": "cloud", "status": "unknown"}
        }
        self.transport.session_guard.on_change = self._async_protection_changed
        self.transport.on_observation = self._async_native_observation

    @property
    def resolved_device_identity(self):
        """Expose cached detection results for editable configuration defaults."""
        return self.cloud.cached_device_identity if self.cloud is not None else {}

    def _async_native_observation(self, state, observed_at):
        """Publish actual TCP measurements immediately instead of waiting for polling."""
        if self.local and not self.transitioning and not self._closed:
            self.async_set_updated_data(
                {
                    self.serial: native_data(state, observed_at)
                    | {
                        "power_guard_error": self.transport.session_guard.error,
                    }
                }
            )
            self.energy_polling.wake()
            self.configuration_polling.wake()

    def _async_protection_changed(self):
        """Immediately expose and notify protective actions independently of polling."""
        if self._closed:
            return
        guard = self.transport.session_guard
        self.async_update_listeners()
        if not guard.error:
            return
        from homeassistant.components import persistent_notification

        details = f"Wallbox {self.serial}: {guard.error}."
        if guard.requested_at_violation is not None:
            details += f" Requested limit: {guard.requested_at_violation:g} kW."
        if guard.measured_at_violation is not None:
            details += f" Measured power: {guard.measured_at_violation:g} kW."
        persistent_notification.async_create(
            self.hass,
            details,
            title="GoodWe wallbox charging protection",
            notification_id=f"sems_wallbox_{self.entry.entry_id}_power_protection",
        )

    async def async_initialize(self):
        """Load intent and recover old ownership without starting charging."""
        await self.connection_intent.async_load()
        self.cloud_restored_at = await self._handover_store.async_load()
        await self.endpoint.async_load()
        if self.endpoint.journal:
            if not self.connection_intent.manual_tcp and self.automatic_fallback.enabled:
                # A crash can precede saving the automatic hint after takeover.
                await self.connection_intent.async_automatic(True)
            await self._mark_cloud_handover()
            await self.endpoint.async_restore()
        await self.charge_mode_policy.async_load()
        await self.charge_mode_policy.async_seed_power(self.initial_power)
        self.transport.accepting = False
        await self.transport.async_listen("0.0.0.0", self.port)
        if self.connection_intent.manual_tcp:
            self.automatic_fallback.pause()
            await self._set_local(True)
        elif self.connection_intent.automatic_tcp and self.automatic_fallback.enabled:
            # A prior known outage must not restart the normal 90-second debounce.
            # Require post-startup cloud data, not an old cached success.
            if self.cloud_restored_at is None:
                await self._mark_cloud_handover()
            self.automatic_fallback.trial_deadline = (
                time.monotonic() + self.automatic_fallback.TRIAL_TIMEOUT
            )
            self.automatic_fallback.reason = "startup_cloud_trial"
        self.automatic_fallback.start()

    async def _mark_cloud_handover(self):
        """Persist the freshness boundary across unload/reload and failed recovery."""
        async with self._handover_lock:
            self.cloud_restored_at = time.time()
            await self._handover_store.async_save(self.cloud_restored_at)

    async def _set_local(self, enabled):
        if enabled == self.local and self.endpoint.journal is None:
            return
        if (settings := getattr(self, "cloud_settings", None)) is not None:
            settings.invalidate()
        self.transition_target = "tcp" if enabled else "cloud"
        self.transitioning = True
        self.last_update_success = False
        self.async_update_listeners()
        self.write_confirmation.cancel()
        self.routing_epoch += 1
        self.update_interval = timedelta(seconds=5)
        try:
            if enabled:
                if self.local:
                    return
                if self.endpoint.journal:
                    await self.endpoint.async_restore()
                try:
                    self.transport.accepting = True
                    await self.endpoint.async_activate()
                    async with asyncio.timeout(35):
                        while not self.transport.available:
                            await asyncio.sleep(0.1)
                    await self.transport.async_command("status")
                    self.local = True
                except BaseException:
                    # A failed/cancelled takeover must attempt its recorded rollback.
                    await self._mark_cloud_handover()
                    try:
                        await self.endpoint.async_restore()
                    finally:
                        await self.transport.async_disconnect()
                    raise
            else:
                guard_task = self.transport.session_guard.task
                if guard_task is not None and not guard_task.done():
                    # Keep Socket A owned until the bounded protective Stop finishes.
                    # Moving it first would interrupt independent Stop verification.
                    await asyncio.shield(guard_task)
                await self._mark_cloud_handover()
                self.transport.expected_disconnect = True
                try:
                    await self.endpoint.async_restore()
                    await self.transport.async_disconnect(expected=True)
                    self.local = False
                except BaseException:
                    # Rollback can fail after the peer left. Do not hide lost ownership.
                    if not self.transport.available:
                        self.transport.session_guard.report_issue(
                            "Cloud handover failed; current charging state is unverified"
                        )
                    raise
                finally:
                    self.transport.expected_disconnect = False
            # Old telemetry must not appear as confirmation from the new transport.
            self.last_update_success = False
            self.async_update_listeners()
        finally:
            self.transitioning = False
            self.transition_target = None
            self.async_update_listeners()

    async def async_reauthenticate(self, username, password):
        """Refresh shared cloud credentials without relinquishing TCP ownership."""
        async def replace():
            push = getattr(self, "cloud_push", None)
            if push is not None:
                await push.close()
            from .operation_budget import async_execute

            await async_execute(self.hass, self.cloud.replace_credentials, username, password)
            self.cloud_settings.invalidate()
            self.automatic_fallback.blocked = False
            if self.automatic_fallback.reason == "authentication_failed":
                self.automatic_fallback.reason = None
            # Preserve paused/manual preference; only an automatic trial may resume.
            self.automatic_fallback.next_attempt = 0
            if push is not None:
                from .cloud_push import CloudPush

                self.cloud_push = CloudPush(self, self.serial, self.cloud.fetch_mqtt_settings)
                self.cloud_push.start()
            self.async_update_listeners()

        await self.charge_mode_policy.async_setting_write(replace)

    async def async_cloud_preflight(self):
        """Probe cloud reachability without changing routing or entity values."""
        return await async_cloud_preflight(self)

    async def async_set_local(self, enabled):
        """Serialize transport handover against mode, power and Start/Stop."""
        self.automatic_fallback.pause()
        try:
            async def select():
                # Store the explicit preference before mutation; even a failed
                # connection attempt must not silently erase the user's choice.
                await self.connection_intent.async_manual(enabled)
                await self._set_local(enabled)
            await self.charge_mode_policy.async_setting_write(select)
        finally:
            await self.async_refresh()

    @confirmation_read
    async def _async_update_data(self):
        epoch = self.routing_epoch
        try:
            deadline = self.automatic_fallback.trial_deadline
            if not self.local and deadline is not None:
                # Also bound the first setup refresh; slow HTTP must not delay
                # entity setup beyond the short cloud verification window.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Cloud verification budget expired")
                async with asyncio.timeout(min(10, remaining)):
                    result = await self._async_read_data()
            else:
                result = await self._async_read_data()
            if self._closed or self.transitioning or epoch != self.routing_epoch:
                raise UpdateFailed("Observation belongs to an inactive transport")
        except (CloudAuthenticationError, OSError, ValueError, RuntimeError, UpdateFailed) as exc:
            if self._closed or self.transitioning or epoch != self.routing_epoch:
                raise UpdateFailed("Observation belongs to an inactive transport") from exc
            if (push := getattr(self, "cloud_push", None)) is not None:
                push.polling.failed()
            if self.local and not self.transitioning and epoch == self.routing_epoch:
                self.automatic_fallback.local_observation(False)
            if not self.local and not self.transitioning and epoch == self.routing_epoch:
                if isinstance(exc, CloudAuthenticationError):
                    self.entry.async_start_reauth(self.hass)
                self.automatic_fallback.observation(
                    False,
                    "authentication_failed" if isinstance(exc, CloudAuthenticationError)
                    else "cloud_unavailable",
                    blocked=isinstance(exc, CloudAuthenticationError),
                )
            raise UpdateFailed(str(exc)) from exc
        if self.local and epoch == self.routing_epoch:
            self.automatic_fallback.local_observation(True)
            # Status command has released its transport lock at this point.
            self.energy_polling.wake()
            self.configuration_polling.wake()
        if not self.local and epoch == self.routing_epoch:
            await self.connection_intent.async_automatic(False)
            if self._closed or self.transitioning or epoch != self.routing_epoch:
                raise UpdateFailed("Observation belongs to an inactive transport")
            self.automatic_fallback.observation(True)
        return result

    async def _async_read_data(self):
        self.cloud_settings.request_refresh()
        if self.transitioning or self._closed:
            raise UpdateFailed("Transport transition in progress")
        if self.local:
            # The device's unsolicited reports can be tens of seconds apart.
            # Active protection needs fresh measurements, not repeated cached data.
            active = self.transport.session_guard.limit is not None
            self.update_interval = timedelta(seconds=2 if active else 5)
            if not self.transport.available:
                raise UpdateFailed("Native wallbox telemetry is unavailable")
            if time.monotonic() - self.transport.observed_at >= 1:
                observed_before = self.transport.observed_at
                session_epoch = self.transport.epoch
                try:
                    await self.transport.async_command("status", timeout=10)
                except TimeoutError as exc:
                    # A queued query can expire while another command owns the
                    # lock. Accept only a newer valid report from the same live
                    # session, received independently during this refresh.
                    if not (
                        self.transport.available
                        and self.transport.epoch == session_epoch
                        and self.transport.observed_at > observed_before
                    ):
                        raise UpdateFailed(
                            "Fresh native wallbox telemetry is unavailable"
                        ) from exc
                except (ConnectionError, ValueError) as exc:
                    raise UpdateFailed(
                        "Fresh native wallbox telemetry is unavailable"
                    ) from exc
            return {
                self.serial: native_data(
                    self.transport.latest, self.transport.observed_at
                )
                | {"power_guard_error": self.transport.session_guard.error}
            }
        if self.cloud is None:
            raise UpdateFailed(
                "Cloud connection restored; no SEMS account configured here"
            )
        epoch = self.routing_epoch
        try:
            timestamped = (
                getattr(self.cloud, "supports_timestamped_observation", False) is True
            )
            reader = (
                self.cloud.fetch_status_observation
                if timestamped
                else self.cloud.get_data_gen2
            )
            data = await self.hass.async_add_executor_job(reader, self.serial)
            if epoch != self.routing_epoch:
                raise UpdateFailed("Cloud response belongs to an earlier transport")
            if not isinstance(data, dict) or data.get("sn") != self.serial:
                raise UpdateFailed("No matching cloud device report")
            if "offline" in str(data.get("status", "")).lower():
                raise UpdateFailed("SEMS reports the wallbox offline")
            if self.automatic_fallback.enabled:
                try:
                    age = report_age(data, self.hass.config.time_zone, time.time())
                except ValueError as exc:
                    raise UpdateFailed(str(exc)) from exc
                # Conservative ten-minute ceiling; never call a frozen report healthy.
                # This is independent of the debounce on completed failed polls.
                if age > 600:
                    raise UpdateFailed("Cloud device report is older than ten minutes")
            if self.cloud_restored_at is not None:
                # A fresh report must postdate handover; an established TCP link is insufficient.
                from datetime import datetime

                from .charge_mode_adapter import _marker

                marker = _marker(data.get("lastUpdate"))
                if marker is None:
                    raise UpdateFailed("Cloud report lacks a timestamp")
                stamp = datetime.fromisoformat(marker.split(":", 1)[1])
                if stamp.tzinfo is None:
                    from zoneinfo import ZoneInfo

                    # SEMS local timestamps are interpreted in the configured HA timezone.
                    stamp = stamp.replace(tzinfo=ZoneInfo(self.hass.config.time_zone))
                if stamp.timestamp() > time.time() + 5:
                    raise UpdateFailed(
                        "Cloud report timestamp is in the future; check HA timezone"
                    )
                if stamp.timestamp() < self.cloud_restored_at:
                    raise UpdateFailed(
                        "Cloud has not published fresh data after handover"
                    )
                async with self._handover_lock:
                    if epoch != self.routing_epoch:
                        raise UpdateFailed(
                            "Cloud response belongs to an earlier transport"
                        )
                    await self._handover_store.async_save(None)
                    self.cloud_restored_at = None
            last = (
                None
                if timestamped
                else await self.hass.async_add_executor_job(
                    self.cloud.fetch_last_charge, self.serial
                )
            )
            if epoch != self.routing_epoch:
                raise UpdateFailed("Cloud response belongs to an earlier transport")
            data = dict(data, transport="cloud")
            if last:
                data.update(last)
                data["power"] = (
                    last.get("last_charge_power")
                    if last.get("last_charge_work_status") == 6
                    else 0
                )
            charging = data.get("last_charge_work_status") == 6 or str(
                data.get("status", "")
            ).lower() in ("charging", "evdetail_status_title_charging")
            interval_key = "scan_interval_charging" if charging else "scan_interval"
            seconds = self.entry.options.get(
                interval_key, self.entry.data.get(interval_key, 30 if charging else 60)
            )
            push = getattr(self, "cloud_push", None)
            self.update_interval = (
                push.polling.observed(data, seconds) if push is not None
                else timedelta(seconds=seconds)
            )
            self.cloud_settings.observe_mode(data.get("chargeMode"))
            return {self.serial: data}
        except CloudAuthenticationError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise UpdateFailed(str(exc)) from exc

    async def async_shutdown(self):
        """Restore the owned cloud endpoint before closing the listener."""
        if self._closed:
            return
        self.write_confirmation.close()
        if (push := getattr(self, "cloud_push", None)) is not None:
            await push.close()
        await self.cloud_settings.close()
        await self.energy_polling.close()
        await self.configuration_polling.close()
        await self.control_fallback.close()
        await self.automatic_fallback.close()
        await self.charge_mode_policy.async_setting_write(
            lambda: self._set_local(False)
        )
        await self.charge_mode_policy.async_close()
        self._closed = True
        await self.transport.async_close()
        await super().async_shutdown()

    async def async_abort_setup(self):
        """Release all local resources after failed setup, retaining recovery journals."""
        operations = []
        if (push := getattr(self, "cloud_push", None)) is not None:
            operations.append(push.close)
        operations.extend((
            self.cloud_settings.close, self.energy_polling.close, self.configuration_polling.close,
            self.control_fallback.close, self.automatic_fallback.close,
            self.charge_mode_policy.async_close, self.transport.async_close,
            super().async_shutdown,
        ))
        self.write_confirmation.close()
        self._closed = True
        for operation in operations:
            try:
                cleanup = asyncio.create_task(operation())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        # Setup propagates its original failure after cleanup.
                        # Repeated caller cancellation must not interrupt a close.
                        continue
                cleanup.result()
            except BaseException:
                # Setup already failed (possibly cancelled); one cleanup failure
                # must not orphan the remaining tasks or listening socket.
                _LOGGER.exception("Failed to release a resource after native setup failure")
