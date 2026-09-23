# GoodWe Wallbox -- Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Tests](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/actions/workflows/tests.yml)
[![Validate](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/actions/workflows/validate.yml/badge.svg)](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/actions/workflows/validate.yml)
[![GitHub release](https://img.shields.io/github/release/prezervos/goodwe-wallbox-sems-home-assistant.svg)](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/releases)

Home Assistant custom integration for the **GoodWe Wallbox**.

Supports cloud, local Modbus and optional native Socket A TCP connections:

| Mode | Chargers | How it works | Internet required |
|------|----------|-------------|-------------------|
| **Local Modbus TCP** | gen 2 only | Connects directly to the wallbox over your LAN using Modbus TCP | No |
| **SEMS cloud** | gen 1, gen2 | Polls the SEMS / SEMS Plus API using the account gateway | Yes |
| **Native Socket A TCP** | GW11 HCA physically tested | Local TCP control with optional automatic cloud fallback; distinct from Modbus | No for local control |

---

## Features

### Cloud and Modbus modes

| Entity | Type | Description |
|--------|------|-------------|
| Charging | Switch | Start / stop charging |
| Charge mode | Select | Fast / PV priority / PV & battery |
| Status | Sensor | Current charging state |
| Vehicle state | Sensor | Car plug connection state |
| Charging power | Sensor (kW) | Actual power drawn |
| Session energy | Sensor (kWh) | Energy delivered in current session |
| Charge duration | Sensor (min) | Duration of current / last session |
| Charge power limit | Number (kW) | Set max charge power |
| Ensure minimum power | Switch | Device minimum-power policy; availability and writable modes depend on model/transport. Tested original HCA changes require idle. |

### Additional Modbus entities

| Entity | Type | Description |
|--------|------|-------------|
| Charging station status | Sensor (Enum) | Detailed 12-state status from register 10017; attributes: all fault/warning register bits decoded |
| Car connection | Sensor (Enum) | Plug / CP state (disconnected / half-connected / connected) |
| Fault state | Sensor (Enum) | Aggregated ok / warning / fault with decoded bit attributes |
| Communication status | Sensor | Active connections (Wi-Fi, IoT cloud, inverter, meters, EMS) |
| Charge start mode | Sensor (Enum, diag) | How the session was started: Plug&Charge, backend, auth card, VIN... (reg 10076) |
| Charging strategy | Sensor (Enum, diag) | Auto full / fill by time / fixed amount / by energy (reg 10077) |
| Reservation | Sensor (Enum, diag) | Whether a scheduled reservation is active (reg 10079) |
| Power source | Sensor (Enum, diag) | Energy source during charging: grid / PV / battery / combinations (reg 10108) |
| Phase A/B/C voltage | Sensor (V) | Per-phase AC voltage |
| Phase A/B/C current | Sensor (A) | Per-phase AC current |
| Max charge power | Number (kW) | Register 10029 limit (range depends on hardware model) |
| Max session energy | Number (kWh) | Stop after delivering this energy (0 = unlimited) |
| Min session energy | Number (kWh) | Keep charging until this energy is delivered |
| Battery discharge SOC | Number (%) | Discharge limit for PV+battery mode |
| Plug & Charge | Switch | Enable automatic charging on plug-in |
| Dynamic load management | Switch | Enable DLM current redistribution |
| EMS minimum power mode | Switch | Force minimum power dispatch via EMS |

### Optional settings and transport support

Cloud integrations also retain capability-dependent controls for grid current,
output power, session energy targets, battery SOC, completion time, phase switching,
dynamic load management and Plug & Charge. Availability depends on the model and
actual API values; a successful API acknowledgement alone does not prove the
wallbox applies a setting.

Enabling native TCP keeps the core Start/Stop, mode, power and session-energy
identities. Extended cloud-only settings become unavailable while TCP owns the
connection. The verified original-HCA minimum-power control is an exception: its
state is available in all modes, but writes require idle. On the tested HCA,
cloud PV-mode writes and idle native writes are verified; the cloud Fast-mode
setter is ineffective and reports a guarded error. SolarGo Auto start works on
that device, but its portable cloud/TCP control is not established.

Entity and service-error catalogs include English (`en`), Czech (`cs`), German (`de`) and Spanish (`es`). Hardware support is separate from translation coverage.

---

## Requirements

- Tested baseline: Home Assistant 2026.9.2 with Python 3.14; production checked on 2026.9.3. Earlier versions are not currently validated.
- GoodWe EV Charger
- For **Modbus mode**: wallbox reachable on your LAN, port 502 open, must be enabled in SolarGo
- For **SEMS cloud mode**: SEMS / SEMS Plus account with the wallbox registered

---

## Installation

### Via HACS (recommended)

1. Open HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/prezervos/goodwe-wallbox-sems-home-assistant` as **Integration**
3. Search for **GoodWe Wallbox** and download it
4. Restart Home Assistant

### Manual

Copy the `custom_components/sems_wallbox/` folder into your HA `config/custom_components/` directory and restart.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for **GoodWe Wallbox**.

The first step asks you to choose a connection type.

---

## Option A: Local Modbus TCP (supported generation 2 models)

This mode communicates directly with the wallbox over your local network. No cloud account is needed and it exposes more entities than the cloud mode.

Cloud and local connectivity depend on the model and its communication configuration. Native Socket A TCP is a separate option described below; do not enable Modbus on the assumption that it is the same protocol.

### Prerequisites

- The wallbox must have a static IP address -- set a DHCP reservation in your router.
- Port **502** must be reachable from Home Assistant.
- TCP Modbus must be enabled in Communication menu of Solar Go - Wallbox.

### Setup steps

1. Choose **Local Modbus TCP** in the connection type step.
2. Enter the wallbox **IP address** (e.g. `192.168.1.50`) and **port** (default `502`).
3. Leave **Modbus device ID** as `0` to auto-detect -- the integration scans common IDs and finds the wallbox automatically. GoodWe Wallbox Gen2 uses ID **247 (0xF7)**; enter it directly to skip the scan.
4. The integration reads the serial number directly from the device and creates the entry automatically.

### Finding the IP address

- Your router's DHCP client list -- look for a device named `5011K......`.
- The Solar Go Comunication menu.
- A LAN scanner: `nmap -sn 192.168.1.0/24`.

---

## Option B: SEMS cloud

This mode polls the SEMS / SEMS Plus API. An internet connection and a SEMS account are required; the client validates the regional gateway supplied during login.

### Account permissions

Use an account that can view and control the registered wallbox. A shared/visitor
account needs control permissions; read-only access does not establish Start/Stop
support. Sharing menus and permissions vary by SEMS application and account.
Cloud commands and optional MQTT discovery share the integration's session and
login backoff. Separate HA installations and phone apps have their own sessions.

### Setup steps

1. Choose **SEMS cloud** in the connection type step.
2. Enter your **SEMS Plus / semsportal.com** username and password.
3. If you have multiple plants, select the one that contains the wallbox.
4. Confirm the detected wallbox (or enter the serial number manually if auto-detection fails).
5. The integration stores the Plant ID and product model automatically.


---

## Update interval

Default polling: **60 s** idle, **30 s** while charging. Adjust via  
**Settings → Devices & Services → GoodWe Wallbox → Configure**.

---

## Debugging

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.sems_wallbox: debug
```

---

## Development

Run from the repository root in an isolated environment, not from inside
`custom_components/sems_wallbox` (its `select.py` can shadow Python's standard library):

```sh
python -m pip install pytest pytest-asyncio requests voluptuous aiomqtt==2.5.1
python -m pytest tests/ -q
```

See [development validation](docs/DEVELOPMENT.md) for real Home Assistant lifecycle,
service, translation and loopback protocol tests. Unit tests use HA stubs and do
not prove physical support for an untested charger.

## Upgrade notes

Read [the draft release notes](docs/RELEASE_NOTES_DRAFT.md) before installing this
unreleased candidate. Both current upstream and this candidate use `sems_wallbox`.
Historical `sems-wallbox` installations require separate migration. Entity IDs and
custom names are preserved for the reviewed baseline, but default labels, categories,
missing-data behavior and some state semantics changed. Check dependent templates;
do not treat unavailable measurements as zero.

## Changelog

### Unreleased

- Opt-in native Socket A TCP control and automatic cloud-failure fallback.
- Persistent native charging preferences, verified handovers and power supervision.
- Shared cloud authentication, bounded login recovery and supplemental MQTT hints.
- Native session energy, optional lifetime energy, truthful missing-data handling,
  translated service errors and expanded lifecycle/protocol tests.

See [release notes and migration caveats](docs/RELEASE_NOTES_DRAFT.md). No new
release version or GitHub publication has been created yet.

### 2.0.0
- **Local Modbus TCP mode**: connect directly to the wallbox without cloud or internet
  - Auto-detects Modbus device ID (scans common IDs, GoodWe Gen2 uses 247/0xF7)
  - 10 writable registers: start/stop, charge mode, max power, session energy limits, battery SOC threshold, Plug&Charge, dynamic load management, EMS dispatch
  - New sensors: charging station status (12 states), car connection, per-phase voltage/current, total energy, fault state with decoded bit attributes
  - New controls: max charge power, max/min session energy, battery discharge SOC, Plug&Charge, dynamic load management, EMS minimum power mode
- **Integration renamed** to "GoodWe Wallbox" (domain `sems_wallbox`)
- Config flow now shows a connection type choice (cloud vs. Modbus) as the first step

### 1.4.0
- `getLastCharge` polling: each coordinator update also calls `getLastCharge` to get the real charging state
- **Status sensor** driven by `workStu=6` from `getLastCharge` -- correctly shows Charging in all PV modes
- **Charging power sensor** shows `pevChar` (actual drawn power) instead of the configured limit
- **Session energy sensor** reads `currentChargeQuantity` from `getLastCharge`
- **New: Allocated charge power sensor** -- readonly, shows inverter's dynamic allocation
- **New: Charge duration sensor** -- current/last session duration in minutes
- **Charge power limit slider** always visible; moving it from any mode switches to Fast
- 128 unit tests, all passing

### 1.3.0
- Visitor account support in config flow
- Auto-discovery of `productModel` via EU gateway
- set-mode timeout raised to 90 s

### 1.2.0
- Auto-discovery of plants and EV chargers in config flow
- Full Gen2 / HCA series support via SEMS Plus EU gateway
- Dynamic polling (faster while charging)
- Options flow for Plant ID, product model and polling intervals

### 1.1.0
- Full Czech and English entity translations
- `SemsWorkStateSensor` -- vehicle connection state
- Charge power slider disabled when charge mode is not Fast
- Grace period logic in charging switch (130 s)

### 1.0.0
- Initial release by [@prezervos](https://github.com/prezervos)

## Credits

Based on the original work by [@prezervos](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant),  
which was itself inspired by [@TimSoethout/goodwe-sems-home-assistant](https://github.com/TimSoethout/goodwe-sems-home-assistant).

## Remember charging preferences

The optional **Restore the mode selected in HA before charging** setting captures
the current reported mode when enabled on the final options page. If the current
mode cannot be verified, enabling it is rejected. Later explicit HA mode selections
update the saved preference; changes in SolarGo remain visible but do not replace it.
Before an HA Start, the enabled policy restores and verifies the saved mode.
Setup, reload and periodic polling never force a mode or start charging. With the
option disabled, Start uses the current device mode. There is no separate initial-mode
field in configuration.

For entries with native TCP enabled, requested power is saved across reloads and
restarts independently of the mode-restoration checkbox. Native TCP always uses
that saved ceiling for its protection policy; without one it uses the
identified model minimum (4.2 kW for the tested GW11 HCA, 1.4 kW for the 7 kW profiles).
The 7/22 kW profiles have protocol tests, not equivalent physical validation.
Legacy cloud-only and Modbus entries enable the saved mode/power policy through
the mode-restoration option; they do not provide the same unconditional saved-power
behavior as native-enabled entries.

In the existing cloud path, changing the power number also selects Fast. In the
native TCP path, the power number preserves the selected mode, and the saved
power ceiling applies to Fast, PV priority and PV + battery. The number shows the
requested value; `reported_power_limit` and the power sensor show the distinct
device setpoint and actual consumption. These quantities need not be identical.

## Option C: Native Socket A TCP (experimental, opt-in)

Enable **Native TCP controls** in an existing cloud entry's options to keep its
credentials and core entity identities. Alternatively, create a native-only entry.
Supply the wallbox IPv4 address, the HA address reachable from the wallbox, and a
published listener port (default 18899). Optional UDP discovery verifies the
serial at the supplied address. No household address or serial is hard-coded.

The **Local TCP connection** switch takes over Socket A; switching it off restores
the recorded cloud destination. This is a manual transport selection, distinct
from Modbus. Socket B must be disabled. An explicit TCP choice is remembered and
restored after HA restart/reload; it does not wait for cloud availability. Selecting
cloud clears that preference. Reload can still cause a short physical reconnect.
When TCP came from automatic fallback instead, reload permits up to 60 seconds
for fresh cloud data before retrying TCP, without the normal outage debounce.
Native-only entries have no cloud telemetry until TCP is selected for the first
time. Native controls retain charging preferences but never replay Start on reload.

### Automatic fallback

Open the entry's **Configure** dialog, enable **Native TCP controls (development)**
on page 1, then continue to **TCP and automatic fallback (2/2)**. Enable
**Automatically use TCP when cloud fails** and confirm page 2 to save both pages.
This requires a cloud account and defaults to off.

Normal automatic takeover requires at least three failed observations and 90 seconds
from the first detected failure. Frozen but successful cloud reports have a separate
freshness timeout, so this is not a universal 90-second outage guarantee. A rejected
login requests reauthentication rather than triggering takeover. An unsent Start/Stop
can use the separately verified control-failure fallback when opted in; uncertain
commands are not blindly replayed.

After 30 minutes on automatic TCP, an idle wallbox can attempt cloud return if the
preliminary reachability/session check passes. Fresh wallbox data must then arrive
within 60 seconds; failed trials back off up to two hours. During switching,
measurements can be unavailable and Active transport shows progress. Core controls
retain only the latest intent for each setting. Manual TCP stays selected and pauses
automatic return. Selecting cloud manually pauses automatic fallback until reload.

TCP Start/Stop is implemented for all three charging modes. Before Start and after
a stopped mode change, TCP restores the saved power ceiling and verifies a fresh
report. Stop before changing mode. Explicit power changes remain available during
charging. Actual-load supervision follows the latest user request and requests
Stop on excess load. It cannot protect a session after the HA host is lost.

PV waiting is shown separately from measured charging. Preserving a PV mode and
power ceiling does not establish solar-only charging when SEMS is disconnected;
this integration does not calculate solar surplus or implement a PV regulator.

Physical validation covers GW11 HCA Start/Stop and power changes in all modes.
GW7 HCA/ACA and GW22 HCA have model-range/protocol tests, not equivalent hardware
coverage. Automatic fallback is opt-in and requires cloud credentials. Bluetooth, history
import and decoded fault meanings are not implemented in this path. Auxiliary settings are exposed when device capabilities and reported values support them. Cloud-only settings become unavailable in TCP; verified native minimum-power control is the exception. The existing session-energy sensor also reads verified HCA TCP telemetry in kWh; it resets to zero after Stop, as reported by the device. Cloud and TCP use their own observations, never an estimated integral or lifetime-counter difference.

See [native TCP behavior and architecture](docs/NATIVE_TCP.md) for configuration,
recovery, supervision limits and protocol details, and [development validation](docs/DEVELOPMENT.md)
for reproducible checks.

### Optional native lifetime energy

Enable the diagnostic **Wallbox total energy** entity to read the verified native
cumulative counter. It is disabled by default, uses kWh and `total_increasing`,
and is independent of the legacy cloud session-energy sensor. Polling occurs
only in idle TCP mode, at most every five minutes and after an observed session
ends. Cloud/missing/invalid data is unavailable, never zero. Updates can arrive
after a cloud interval or after charging ends, so do not use their timestamps for
precise tariff allocation. Counter integrity and simulated reset behavior are
tested; a physical factory reset has not been tested.

State-class conventions: https://developers.home-assistant.io/docs/core/entity/sensor/

### Supplemental cloud notifications

Cloud connections automatically attempt the GoodWe MQTT event service over TLS.
An event requests a fresh authoritative cloud read; it never supplies entity values
or changes charging settings. Your polling intervals remain the baseline when
notifications are unavailable or unverified. A recently verified telemetry stream
can temporarily double the backup interval; silence for one original interval
restores normal polling and triggers a read. No local MQTT broker or manual broker credentials
are needed. Notifications are suspended while using local TCP. Download integration
diagnostics to see whether the optional event connection is established.

Delivery timing depends on GoodWe and is not guaranteed for every state change.
See [validation and known limitations](docs/VALIDATION.md) for measured limitations.
