"""Keep settings tests on the real shared API guard with mocked device I/O."""

import importlib
import threading
from types import MethodType


def bind_settings_api(api, package):
    """Install real orchestration while preserving caller-owned read/write mocks."""
    cls = importlib.import_module(package + ".sems_api").SemsApi
    api._closed = False
    api._web_request_lock = threading.RLock()
    api._pending_mode_edits = {}
    outcome = importlib.import_module(package + ".cloud_command").SettingOutcome
    # These fixture writes model conclusive acknowledgements/rejections. Tests
    # of uncertain delivery use the real sender with mocked HTTP responses.
    def send(*args, **kwargs):
        return (outcome.ACKNOWLEDGED if api.set_charge_mode_gen2(*args, **kwargs)
                else outcome.REJECTED)
    api._send_charge_mode_gen2 = send
    for name in ("_confirm_previous_mode_edit", "_track_mode_setting", "edit_mode_parameter", "set_minimum_power_checked"):
        setattr(api, name, MethodType(getattr(cls, name), api))
