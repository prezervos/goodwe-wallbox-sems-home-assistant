"""Preserve cloud command failures without implying that a write was not applied."""

from __future__ import annotations

from enum import Enum
import re


class CloudCommandError(RuntimeError):
    """A failed cloud acknowledgement with bounded, non-sensitive diagnostics.

    Args:
        action: Start or stop operation.
        code: Server code or a fixed transport-failure code.
        category: Server category, retained only when it is a bounded identifier.
        uncertain: Whether readback is needed to establish the device outcome.
    """

    def __init__(self, action, code, category=None, *, uncertain=False):
        self.action = action
        self.code = str(code) if re.fullmatch(r"[A-Za-z0-9_]{1,40}", str(code)) else "unknown"
        self.category = category if isinstance(category, str) and re.fullmatch(
            r"[a-z_]{1,80}", category) else None
        self.cloud_command_uncertain = uncertain
        outcome = "outcome unknown; command may have applied" if uncertain else "rejected"
        super().__init__(f"Cloud {action} {outcome} ({self.code})")


class CloudSettingError(CloudCommandError):
    """An uncertain settings write that must be reconciled before another edit."""

    def __init__(self, code):
        super().__init__("setting", code, uncertain=True)


class SettingOutcome(Enum):
    """Conclusive outcomes; uncertain delivery raises CloudSettingError."""

    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    NOT_SENT = "not_sent"
