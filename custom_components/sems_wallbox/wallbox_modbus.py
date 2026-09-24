"""Modbus TCP client for the GoodWe AC EV Charger Gen2.

Protocol doc: AC EV Charger 2nd Gen Modbus Protocol v1.0.15
Connection: Modbus TCP, port 502, device/unit ID 247 (0xF7).

"""

from __future__ import annotations

from collections import deque
import logging
import math
import threading
import time

from .operation_budget import BudgetCancelled, request_timeout, serialized_request
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Modbus TCP defaults discovered empirically.
DEFAULT_MODBUS_PORT = 502
DEFAULT_MODBUS_DEVICE_ID = 247  # 0xF7
BREAKER_CURRENT_MIN = 0
BREAKER_CURRENT_MAX = 2000  # Household breaker, protocol v1.0.15 register 10026.

# Register base addresses
_REG_FAULT_BASE = 10000
_REG_VOLTAGE_A = 10009
_REG_STATUS = 10017
_REG_COMM_STATUS = 10018
_REG_PLUG_CHARGE = 10019
_REG_RESERVATION = 10020
_REG_MAX_POWER = 10029
_REG_CHARGING_MODE = 10032
_REG_SN = 10040           # STR 8 registers = 16 bytes
_REG_SW_VER_EXT = 10048   # STR 2 registers
_REG_SVN_INT = 10050      # U16 1 register
_REG_WIFI_VER = 10051     # STR 5 registers
_REG_HW_VER = 10056       # STR 2 registers
_REG_POWER_SPEC = 10058   # U16
_REG_PILE_TYPE = 10059    # U16
_REG_CHARGE_DURATION = 10063  # U32 2 registers
_REG_HIST_ENERGY = 10065   # U32 2 registers
_REG_PILE_TIME_YM = 10067  # U16
_REG_CAR_CONN = 10075     # U16
_REG_START_MODE = 10076   # U16
_REG_CP_STATE = 10084     # U16
_REG_GREEN_ENERGY = 10103  # U32 2 registers
_REG_GRID_ENERGY = 10105   # U32 2 registers
_REG_PROJECT_TYPE = 10107  # U16
_REG_POWER_SOURCE = 10108  # U16
_REG_MAINTAIN_MIN = 10024  # U16

STATUS_MAP = {
    0: "idle_no_plug",
    1: "idle_plugged",
    2: "handshaking",
    3: "charging",
    4: "charging_completed",
    5: "abnormal_alarm",
    6: "scheduled_start",
    7: "maintenance",
    8: "start_failed",
    9: "upgrading",
    10: "interrupted",
}

POWER_MAP = {0: "7kW", 1: "11kW", 2: "22kW"}
TYPE_MAP = {0: "three-phase", 1: "single-phase"}
CP_STATE_MAP = {0: "no_voltage", 1: "12V", 2: "9V", 3: "6V", 4: "3V"}


def _decode_str(regs: list[int]) -> str:
    """Decode a list of U16 registers as ASCII string (2 chars per register)."""
    chars = []
    for r in regs:
        hi = (r >> 8) & 0xFF
        lo = r & 0xFF
        if hi:
            chars.append(chr(hi))
        if lo:
            chars.append(chr(lo))
    return "".join(chars).rstrip("\x00").strip()


def _decode_u32(hi: int, lo: int) -> int:
    """Combine two U16 registers into a U32 value (big-endian)."""
    return (hi << 16) | lo


class WallboxModbusClient:
    """Synchronous Modbus TCP client for GoodWe AC Wallbox Gen2.

    Intended to be called from executor threads (async_add_executor_job).
    Connections are serialized and closed after each read or write operation.
    """

    def __init__(self, host: str, port: int = DEFAULT_MODBUS_PORT,
                 device_id: int = DEFAULT_MODBUS_DEVICE_ID, *, expected_serial: str | None = None) -> None:
        self._host = host
        self._port = port
        self._device_id = device_id
        self._expected_serial = expected_serial
        self._closed = False
        # Prevent this integration's polling and controls from opening overlapping
        # connections. Other clients and firmware connection limits are external.
        self._lock = threading.Lock()
        self._trace_lock = threading.Lock()
        self._events = deque(maxlen=128)
        self._read_requests = 0
        self._write_requests = 0
        self._connection_attempts = 0

    def _trace(self, event: str, **fields: int | float | str) -> None:
        """Record bounded protocol metadata, never register payloads or identity."""
        with self._trace_lock:
            self._read_requests += event == "read_request"
            self._write_requests += event == "write_request"
            self._connection_attempts += event == "connection_attempt"
            self._events.append({"at": time.monotonic(), "event": event, **fields})
        _LOGGER.debug("Modbus trace %s %s", event, fields)

    def diagnostics(self) -> dict[str, Any]:
        """Return a thread-safe cached trace without making any device requests.

        Returns:
            Since-load request counts and the last 128 protocol events.
        """
        with self._trace_lock:
            now = time.monotonic()
            return {
                "connection_attempts": self._connection_attempts,
                "read_requests": self._read_requests,
                "write_requests": self._write_requests,
                "recent_events": [
                    {"age_seconds": round(max(0, now - item["at"]), 3),
                     **{key: value for key, value in item.items() if key != "at"}}
                    for item in self._events
                ],
            }

    def _close_client(self, client) -> None:
        """Close an operation's connection and record the local close attempt."""
        try:
            client.close()
        finally:
            self._trace("connection_close")

    def _write_register(self, client, address: int, value: int):
        """Trace exact wire attempts, including rejected or unacknowledged writes."""
        self._trace("write_request", function=6, address=address, value=value)
        try:
            result = client.write_register(address, value, device_id=self._device_id)
        except Exception:  # Trace transport/library failure without changing propagation.
            self._trace("write_result", address=address, outcome="exception")
            raise
        self._trace("write_result", address=address,
                    outcome="error" if result.isError() else "acknowledged")
        return result

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _make_client(self):
        """Create and connect a fresh ModbusTcpClient. Caller must close it."""
        from pymodbus.client import ModbusTcpClient  # deferred to avoid hard dep at import
        if self._closed:
            raise OSError("Modbus client is closed")
        # Never replay a possibly delivered mutating request after a lost ACK.
        # Read recovery is handled by later coordinator polls.
        client = ModbusTcpClient(self._host, port=self._port, timeout=request_timeout(2), retries=0)
        self._trace("connection_attempt")
        try:
            connected = client.connect()
        except Exception:  # Preserve the library error and close an unreturned client.
            self._trace("connection_failed")
            self._close_client(client)
            raise
        if not connected:
            self._trace("connection_failed")
            self._close_client(client)
            raise OSError(f"Cannot connect to wallbox Modbus at {self._host}:{self._port}")
        self._trace("connection_open")
        return client

    def close(self) -> None:
        """Drain the current operation and reject subsequent connections."""
        with self._lock:
            self._closed = True

    def _verify_write_identity(self, client) -> None:
        """Verify the enrolled serial on the same connection used for a write."""
        registers = self._read(client, _REG_SN, 8)
        if not self._expected_serial or registers is None or _decode_str(registers) != self._expected_serial:
            raise ValueError("Modbus write requires the enrolled device identity")
        # Identity I/O can wait while Stop or shutdown supersedes a pending Start.
        request_timeout(2)

    # ------------------------------------------------------------------
    # Low-level register read
    # ------------------------------------------------------------------

    def _read(self, client, address: int, count: int) -> list[int] | None:
        """Read `count` holding registers starting at `address`."""
        self._trace("read_request", function=3, address=address, count=count)
        try:
            result = client.read_holding_registers(address, count=count,
                                                   device_id=self._device_id)
            if not result.isError():
                self._trace("read_result", address=address, outcome="success")
                return list(result.registers)
            self._trace("read_result", address=address, outcome="error")
            _LOGGER.warning("Modbus read error at %d count=%d: %s", address, count, result)
            return None
        except Exception as exc:  # Library failures are represented as a failed observation.
            self._trace("read_result", address=address, outcome="exception")
            _LOGGER.warning("Modbus read exception at %d: %s", address, exc)
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _write(self, address: int, value: int) -> bool:
        """Write a single holding register (function code 6)."""
        with serialized_request(self._lock):
            try:
                client = self._make_client()
            except OSError as exc:
                _LOGGER.warning("Modbus connect failed for write at %d: %s", address, exc)
                return False
            try:
                self._verify_write_identity(client)
                result = self._write_register(client, address, value)
                if result.isError():
                    _LOGGER.warning("Modbus write error at %d value=%d: %s", address, value, result)
                    return False
                _LOGGER.debug("Modbus write reg=%d value=%d OK", address, value)
                return True
            except (BudgetCancelled, TimeoutError):
                raise
            except Exception as exc:  # The library exposes several protocol/transport failures.
                _LOGGER.warning("Modbus write exception at %d: %s", address, exc)
                return False
            finally:
                self._close_client(client)

    def write_start_stop(self, start: bool) -> bool:
        """Start (True) or stop (False) charging. Reg 10060: 2=start, 1=stop.

        For the start command a pre-reset to 1 is sent first within the same
        TCP connection.  The wallbox firmware sometimes ignores a direct 0→2
        transition; issuing 1→2 (the same sequence the user performs manually
        when pressing OFF then ON) ensures reliable start.
        """
        if not start:
            return self._write(10060, 1)
        # Start: pre-reset to 1 then write 2 in a single connection under the lock.
        with serialized_request(self._lock):
            try:
                client = self._make_client()
            except OSError as exc:
                _LOGGER.warning("Modbus connect failed for write_start_stop: %s", exc)
                return False
            try:
                self._verify_write_identity(client)
                reset = self._write_register(client, 10060, 1)
                if reset.isError():
                    return False
                request_timeout(2)
                result = self._write_register(client, 10060, 2)
                if result.isError():
                    _LOGGER.warning("Modbus start error: %s", result)
                    return False
                _LOGGER.debug("Modbus write reg=10060 value=2 (with pre-reset) OK")
                return True
            except (BudgetCancelled, TimeoutError):
                raise
            except Exception as exc:  # Preserve unknown delivery as failure, never replay Start.
                _LOGGER.warning("Modbus start exception: %s", exc)
                return False
            finally:
                self._close_client(client)

    def write_charge_mode(self, mode: int) -> bool:
        """Set advanced charging mode. Reg 10032: 0=fast, 1=PV, 2=PV+battery."""
        if mode not in (0, 1, 2):
            _LOGGER.warning("Invalid charge mode %d", mode)
            return False
        return self._write(10032, mode)

    def write_max_charge_power(self, kw: float) -> bool:
        """Set max charging power in kW. Reg 10029, SF=10, raw range [14,220]."""
        raw = int(round(kw * 10))
        raw = max(14, min(220, raw))
        return self._write(10029, raw)

    def write_maintain_min_power(self, enabled: bool) -> bool:
        """Enable/disable maintain min charging power. Reg 10024."""
        return self._write(10024, 1 if enabled else 0)

    def write_plug_charge(self, enabled: bool) -> bool:
        """Enable/disable Plug & Charge. Reg 10019."""
        return self._write(10019, 1 if enabled else 0)

    def write_ems_dispatch(self, min_power_mode: bool) -> bool:
        """Set EMS dispatch mode. Reg 10000: 0=normal, 1=minimum power charge."""
        return self._write(10000, 1 if min_power_mode else 0)

    def write_dynamic_load_mgmt(self, enabled: bool) -> bool:
        """Enable/disable dynamic load management. Reg 10025."""
        return self._write(10025, 1 if enabled else 0)

    def write_breaker_current(self, amps: int) -> bool:
        """Set household breaker current, register 10026, integer range 0–2000 A."""
        if (isinstance(amps, bool) or not isinstance(amps, (int, float))
                or not math.isfinite(amps) or not float(amps).is_integer()
                or not BREAKER_CURRENT_MIN <= amps <= BREAKER_CURRENT_MAX):
            raise ValueError("Household breaker current must be a whole number from 0 to 2000 A")
        return self._write(10026, int(amps))

    def write_phase_switch(self, single_phase: bool) -> bool:
        """Enable/disable single-phase mode. Reg 10023: 1=single-phase, 0=three-phase.

        Only meaningful for three-phase (11/22 kW) wallboxes.
        Enabling single-phase allows charging from 1.4 kW instead of the 4.2 kW minimum.
        """
        return self._write(10023, 1 if single_phase else 0)

    def write_max_charge_capacity(self, kwh: float) -> bool:
        """Set max charge capacity per session (energy limit) in kWh. Reg 10027, SF=10."""
        raw = max(0, min(2000, int(round(kwh * 10))))
        return self._write(10027, raw)

    def write_min_charge_capacity(self, kwh: float) -> bool:
        """Set min charge capacity per session (energy minimum) in kWh. Reg 10028, SF=10."""
        raw = max(0, min(2000, int(round(kwh * 10))))
        return self._write(10028, raw)

    def write_battery_discharge_soc(self, pct: int) -> bool:
        """Set battery discharge SOC threshold in %. Reg 10030, range [0,100]."""
        return self._write(10030, max(0, min(100, int(pct))))

    def write_completion_time(self, hours: int) -> bool:
        """Set completion time in hours (reg 10031). 0 = ASAP, 1-6 = hours."""
        return self._write(10031, max(0, min(10, int(hours))))

    @staticmethod
    def detect_device_id(host: str, port: int = DEFAULT_MODBUS_PORT) -> int | None:
        """Scan common Modbus unit IDs and return the first that responds.

        GoodWe Wallbox Gen2 uses device ID 247 (0xF7) by default.
        Other IDs are tried as fallback.
        Returns None if no device responds.
        """
        from pymodbus.client import ModbusTcpClient
        candidates = [247, 1, 2, 0, 255]
        try:
            client = ModbusTcpClient(host, port=port, timeout=3)
            if not client.connect():
                return None
            try:
                for uid in candidates:
                    result = client.read_holding_registers(
                        _REG_STATUS, count=1, device_id=uid
                    )
                    if not result.isError():
                        _LOGGER.debug("Modbus device ID %d responded at %s:%d", uid, host, port)
                        return uid
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("detect_device_id error: %s", exc)
        return None

    def read_all(self) -> dict[str, Any] | None:
        """Read all useful registers and return a normalized data dict.

        A fresh TCP connection is made for each call and closed when done.
        Returns None on communication failure.
        """
        with serialized_request(self._lock):
            try:
                client = self._make_client()
            except OSError as exc:
                _LOGGER.warning("Modbus connect failed: %s", exc)
                return None
            try:
                result = self._read_all_inner(client)
                if result is not None:
                    fields = ("modbus_status_raw", "modbus_power", "set_charge_power",
                              "modbus_car_connected", "modbus_cp_state", "modbus_comm_status",
                              "modbus_charging_on_off", "modbus_start_mode", "modbus_ems_dispatch",
                              "modbus_breaker_current", "modbus_fault_01", "modbus_fault_02",
                              "modbus_fault_03", "modbus_fault_04", "modbus_warn_05",
                              "modbus_warn_06", "modbus_hw_fault_07", "modbus_hw_fault_08")
                    self._trace("observation", **{
                        key: result[key] for key in fields
                        if type(result.get(key)) in (int, float) and math.isfinite(result[key])
                    })
                return result
            finally:
                self._close_client(client)

    def _read_all_inner(self, client) -> dict[str, Any] | None:
        """Internal implementation of read_all."""
        # Block 1: faults + voltages + currents + power + status (10000-10019)
        b1 = self._read(client, 10000, 20)
        if b1 is None:
            return None

        # Block 2: config (10020-10039)
        b2 = self._read(client, 10020, 20)

        # Block 3: SN + versions + power spec + type (10040-10059)
        b3 = self._read(client, 10040, 20)
        if b3 is None:
            return None

        # Block 4: runtime (10060-10079)
        b4 = self._read(client, 10060, 20)
        if b4 is None:
            return None

        # Block 5: CP state + SEMS account (10084-10108), green/grid energy, source
        b5 = self._read(client, 10084, 26)

        # -- Parse block 1 --
        ems_dispatch = b1[0]
        fault_01 = b1[1]
        fault_02 = b1[2]
        fault_03 = b1[3]
        fault_04 = b1[4]
        warn_05 = b1[5]
        warn_06 = b1[6]
        hw_fault_07 = b1[7]
        hw_fault_08 = b1[8]
        voltage_a = b1[9] / 10.0
        voltage_b = b1[10] / 10.0
        voltage_c = b1[11] / 10.0
        current_a = b1[12] / 10.0
        current_b = b1[13] / 10.0
        current_c = b1[14] / 10.0
        charging_power = b1[15] / 10.0   # kW
        session_energy = b1[16] / 10.0   # kWh
        raw_status = b1[17]
        comm_status = b1[18]
        plug_charge = b1[19]

        # -- Parse block 2 (config) --
        reservation_status = b2[0] if b2 else None
        phase_switch = b2[3] if b2 else None
        maintain_min_power = b2[4] if b2 else None
        dynamic_load = b2[5] if b2 else None
        breaker_current = b2[6] if b2 else None
        max_cap = b2[7] / 10.0 if b2 else None
        min_cap = b2[8] / 10.0 if b2 else None
        max_power_raw = b2[9] if b2 else None
        max_power = max_power_raw / 10.0 if max_power_raw is not None else None
        bat_soc_limit = b2[10] if b2 else None
        completion_time = b2[11] if b2 else None
        charging_mode = b2[12] if b2 else None

        # -- Parse block 3 (device info) --
        sn = _decode_str(b3[0:8])
        sw_version = _decode_str(b3[8:10])
        wifi_ble_version = _decode_str(b3[11:16])
        hw_version = _decode_str(b3[16:18])
        power_spec = b3[18]
        pile_type = b3[19]

        # -- Parse block 4 (runtime) --
        charging_on_off = b4[0]   # 1=off, 2=on (reg 10060)
        charge_duration_s = _decode_u32(b4[3], b4[4])
        hist_energy_raw = _decode_u32(b4[5], b4[6])
        hist_energy = hist_energy_raw / 10.0
        car_connection = b4[15]
        start_mode = b4[16]
        charging_strategy = b4[17]
        appointment_sign = b4[19]

        # -- Parse block 5 (extras) --
        cp_state = None
        green_energy = None
        grid_energy = None
        project_type = None
        power_source = None
        if b5:
            cp_state = b5[0]
            # b5[1:19] = SEMS account (18 registers = 36 bytes)
            if len(b5) >= 25:
                green_energy = _decode_u32(b5[19], b5[20]) / 10.0 if len(b5) > 20 else None
                grid_energy = _decode_u32(b5[21], b5[22]) / 10.0 if len(b5) > 22 else None
                project_type = b5[23] if len(b5) > 23 else None
                power_source = b5[24] if len(b5) > 24 else None

        # -- Derive compatibility fields for existing sensor classes --
        # Map raw_status (0-10) to cloud workstate strings
        raw_to_workstate = {
            0: "available_gun_no_inserted",
            1: "available_gun_inserted",
            2: "prepare",
            3: "charging",
            4: "finishing",
            5: "abnormal_alarm",
            6: "available_gun_inserted",
            7: "maintenance",
            8: "suspended_evse",
            9: "upgrading",
            10: "suspended_ev",
        }
        workstate = raw_to_workstate.get(raw_status, "")

        # Map to cloud status strings
        raw_to_status = {
            0: "EVDetail_Status_Title_Waiting",
            1: "EVDetail_Status_Title_Waiting",
            2: "EVDetail_Status_Title_Waiting",
            3: "EVDetail_Status_Title_Charging",
            4: "EVDetail_Status_Title_Waiting",
            5: "EVDetail_Status_Title_Offline",
            6: "EVDetail_Status_Title_Waiting",
            7: "EVDetail_Status_Title_Offline",
            8: "EVDetail_Status_Title_Offline",
            9: "EVDetail_Status_Title_Offline",
            10: "EVDetail_Status_Title_Waiting",
        }
        status_str = raw_to_status.get(raw_status, "EVDetail_Status_Title_Waiting")

        # Emulate last_charge_work_status (6 = charging) for existing power/workstate sensors
        last_charge_work_status = 6 if raw_status == 3 else 0

        # Model string
        model_str = POWER_MAP.get(power_spec, "unknown")
        if pile_type == 1:
            model_str += " single-phase"
        else:
            model_str += " three-phase"

        return {
            # -- compatibility fields --
            "sn": sn,
            "source": "modbus",
            "status": status_str,
            "workstate": workstate,
            "chargeMode": charging_mode,
            "set_charge_power": max_power,
            "charge_from_grid": bool(power_source & 0x01) if power_source is not None else None,
            "ensure_minimum_charging_power": bool(maintain_min_power) if maintain_min_power in (0, 1) else None,
            "scheduleMode": reservation_status,
            "last_charge_work_status": last_charge_work_status,
            "last_charge_power": charging_power,
            "last_charge_energy": round(session_energy, 2),
            "last_charge_duration_minutes": charge_duration_s // 60 if charge_duration_s else 0,
            "name": f"GoodWe Wallbox {sn}",
            "model": model_str,
            "fireware": sw_version,
            # -- modbus-specific fields --
            "modbus_status_raw": raw_status,
            "modbus_status_name": STATUS_MAP.get(raw_status, "unknown"),
            "modbus_voltage_a": voltage_a,
            "modbus_voltage_b": voltage_b,
            "modbus_voltage_c": voltage_c,
            "modbus_current_a": current_a,
            "modbus_current_b": current_b,
            "modbus_current_c": current_c,
            "modbus_power": charging_power,
            "modbus_energy_session": session_energy,
            "modbus_energy_total": hist_energy,
            "modbus_green_energy": green_energy,
            "modbus_grid_energy": grid_energy,
            "modbus_car_connected": car_connection,
            "modbus_cp_state": cp_state,
            "modbus_cp_state_name": CP_STATE_MAP.get(cp_state, "unknown") if cp_state is not None else None,
            "modbus_comm_status": comm_status,
            "modbus_power_source": power_source,
            "modbus_fault_01": fault_01,
            "modbus_fault_02": fault_02,
            "modbus_fault_03": fault_03,
            "modbus_fault_04": fault_04,
            "modbus_warn_05": warn_05,
            "modbus_warn_06": warn_06,
            "modbus_hw_fault_07": hw_fault_07,
            "modbus_hw_fault_08": hw_fault_08,
            "modbus_plug_charge_enabled": bool(plug_charge) if plug_charge in (0, 1) else None,
            "modbus_phase_switch_enabled": bool(phase_switch) if phase_switch in (0, 1) else None,
            "modbus_dynamic_load": bool(dynamic_load) if dynamic_load in (0, 1) else None,
            "modbus_max_charging_power": max_power,
            "modbus_min_capacity": min_cap,
            "modbus_max_capacity": max_cap,
            "modbus_bat_soc_limit": bat_soc_limit,
            "modbus_completion_time": completion_time,
            "modbus_breaker_current": breaker_current,
            "modbus_hw_version": hw_version,
            "modbus_sw_version": sw_version,
            "modbus_wifi_ble_version": wifi_ble_version,
            "modbus_power_spec": power_spec,
            "modbus_pile_type": pile_type,
            "modbus_project_type": project_type,
            "modbus_ems_dispatch": ems_dispatch,
            # reg 10060: 2=charging enabled by HA command, 1=off, 0=not set (e.g. Plug&Charge)
            "modbus_charging_on_off": charging_on_off,
            "modbus_charging_enabled": (charging_on_off == 2),
            "modbus_start_mode": start_mode,
            "modbus_charging_strategy": charging_strategy,
            "modbus_appointment_sign": appointment_sign,
        }
