"""Require a bounded, uninterrupted observation window for TCP Start."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StartObservationWindow:
    """Track primary reports without treating paired frames as new samples.

    This is measured-load observation, not proof of effective current limiting
    or a firmware finalization barrier. Later overload still requires monitoring.
    """

    duration: float = 10.0
    maximum_gap: float = 6.0
    first_at: float | None = None
    last_at: float | None = None
    samples: int = 0

    def observe(self, command: int, observed_at: float, matches: bool) -> bool:
        """Return whether qualifying primary reports span the required window.

        Args:
            command: Native report command; only 104 advances the window.
            observed_at: Monotonic receive time in seconds.
            matches: Whether mode, reported limit and measured load qualify.

        Returns:
            True after uninterrupted qualifying primary observations.
        """
        if not matches:
            self.first_at = self.last_at = None
            self.samples = 0
            return False
        if command != 104:
            return False
        if (
            self.last_at is None
            or observed_at <= self.last_at
            or observed_at - self.last_at > self.maximum_gap
        ):
            self.first_at = observed_at
            self.samples = 0
        self.last_at = observed_at
        self.samples += 1
        return self.samples >= 3 and observed_at - self.first_at >= self.duration
