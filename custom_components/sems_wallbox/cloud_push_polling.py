"""Bounded polling backoff for a recently verified cloud telemetry stream."""

from __future__ import annotations

import math
import time
from datetime import timedelta

from .native_fallback import report_age


class CloudPushPolling:
    """Keep polling unless consecutive telemetry hints produce fresh reports.

    This is a short-lived observation, not a persistent device capability claim.
    Silence is bounded by the user's original interval even while qualified.
    """

    REQUIRED_REPORTS = 3

    def __init__(self, push):
        self.push = push
        self.base_seconds = None
        self.last_verified_at = None
        self.last_marker = None
        self.hint = 0
        self.consumed_hint = 0
        self.hint_at = None
        self.samples = 0
        self.extended = False
        self.epoch = push._epoch()

    def telemetry_hint(self):
        """Record an accepted telemetry hint, without extending any deadline."""
        self.hint += 1
        self.hint_at = time.monotonic()

    def reset(self):
        """Revoke stream qualification without changing TCP polling."""
        self.samples = 0
        self.last_verified_at = None
        self.consumed_hint = self.hint
        self.last_marker = None
        self.extended = False

    def observed(self, data, seconds):
        """Choose the next interval after an authoritative successful cloud read.

        Args:
            data: Validated cloud report for the configured wallbox.
            seconds: Configured polling interval for the current charging state.

        Returns:
            Normal interval or a temporary doubled backup interval.
        """
        now = time.monotonic()
        seconds = float(seconds)
        if self.base_seconds != seconds or self.epoch != self.push._epoch():
            self.reset()
            self.epoch = self.push._epoch()
        self.base_seconds = seconds
        qualified = False
        marker = None
        try:
            wall_time = time.time()
            age = report_age(data, self.push.owner.hass.config.time_zone, wall_time)
            marker = round(wall_time - age, 3)
            fresh = 0 <= age <= seconds
            complete = all(
                not isinstance(data.get(key), bool) and math.isfinite(float(data[key]))
                for key in ("power", "set_charge_power", "chargeMode")
            ) and bool(data.get("status"))
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            fresh = complete = False
        if (
            self.push.connected
            and self.push._eligible()
            and fresh
            and complete
            and self.hint > self.consumed_hint
            and self.hint_at is not None
            and now - self.hint_at <= min(seconds, 10)
            and (self.last_marker is None or marker > self.last_marker)
        ):
            qualified = True
        self.consumed_hint = self.hint
        if qualified:
            if self.last_verified_at is None or now - self.last_verified_at > seconds:
                self.samples = 0
            self.samples = min(self.samples + 1, self.REQUIRED_REPORTS)
            self.last_verified_at = now
            self.last_marker = marker
        elif (
            not fresh
            or not complete
            or not self.push.connected
            or self.last_verified_at is None
            or now - self.last_verified_at >= seconds
        ):
            self.reset()
        self.extended = self.samples >= self.REQUIRED_REPORTS
        return timedelta(seconds=seconds * (2 if self.extended else 1))

    def failed(self):
        """Restore the normal interval before HA schedules a failed-read retry."""
        self.reset()
        if self.base_seconds is not None and self.push._eligible():
            self.push.owner.update_interval = timedelta(seconds=self.base_seconds)

    def check(self):
        """Restore normal polling and request one due read when trust expires.

        Returns:
            True if the caller should request a cloud refresh now.
        """
        if not self.extended:
            return False
        now = time.monotonic()
        owner = self.push.owner
        expired = (
            not self.push.connected
            or not self.push._eligible()
            or not owner.last_update_success
            or self.epoch != self.push._epoch()
            or self.last_verified_at is None
            or now - self.last_verified_at >= self.base_seconds
        )
        if not expired:
            return False
        self.reset()
        if not self.push._eligible():
            return False
        owner.update_interval = timedelta(seconds=self.base_seconds)
        # One refresh also re-arms the public coordinator scheduler immediately.
        # Failed/auth-blocked reads retain the coordinator's own recovery handling.
        entry = getattr(owner, "config_entry", None) or getattr(owner, "entry", None)
        return bool(owner.last_update_success) and not getattr(
            entry, "pref_disable_polling", False
        )

    def diagnostics(self):
        """Return non-sensitive runtime policy facts."""
        return {
            "backup_polling": self.extended,
            "verified_telemetry_reports": self.samples,
            "normal_interval_seconds": self.base_seconds,
        }
