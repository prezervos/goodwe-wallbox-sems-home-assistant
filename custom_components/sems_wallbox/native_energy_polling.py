"""Opt-in idle-only cumulative energy polling."""

from .native_idle_polling import NativeIdlePolling


class NativeEnergyPolling(NativeIdlePolling):
    """Poll verified lifetime counters without changing control availability."""

    TASK_NAME = "GoodWe cumulative energy"
