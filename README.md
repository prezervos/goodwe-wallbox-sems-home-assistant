# GoodWe SEMS Wallbox — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Tests](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/actions/workflows/tests.yml)
[![Validate](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/actions/workflows/validate.yml/badge.svg)](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/actions/workflows/validate.yml)
[![GitHub release](https://img.shields.io/github/release/prezervos/goodwe-wallbox-sems-home-assistant.svg)](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/releases)

Home Assistant custom integration for the **GoodWe EV Charger (Wallbox)** via the [SEMS Portal](https://www.semsportal.com) cloud API.

---

## Features

| Entity | Type | Description |
|--------|------|-------------|
| Status | Sensor (Enum) | Charging / Standby / Offline |
| Vehicle state | Sensor (Enum) | Not Plugged in / Connected / Finished Charging |
| Charging power | Sensor (kW) | Live charge power |
| Total charged energy | Sensor (kWh) | Cumulative energy, compatible with HA Energy Dashboard |
| Charging current | Sensor (A) | Live charge current |
| Charging | Switch | Start / stop charging |
| Charge mode | Select | Fast / PV priority / PV & battery |
| Charge power limit | Number | Set max charge power (4.2–11 kW, 0.1 kW steps); **disabled when mode ≠ Fast** |

All entities are automatically translated — Czech (`cs`) and English (`en`) are included.

---

## Requirements

- Home Assistant 2023.6 or newer  
- A GoodWe **SEMS Plus** account with access to the wallbox (visitor account recommended — see below)  
- GoodWe EV Charger (Wallbox) registered in the SEMS Portal

---

## Recommended: use a visitor account

It is strongly recommended to create a **dedicated visitor account** in the SEMS app and use those credentials for this integration instead of your main account. This way:

- Your main account password is never stored in Home Assistant.
- You can revoke access at any time without changing your main password.
- The integration has only the permissions you explicitly grant.

### How to set it up

1. Open the **SEMS Plus** mobile app and log in with your **main** account.
2. Go to your station (plant) → **Share** (or **Visitor Management**).
3. Tap **Add visitor**, enter the e-mail of the visitor account and set privileges to **Read and Modify** (to allow the integration to start/stop charging and change modes).
4. Register a new account with that visitor e-mail address at [semsportal.com](https://www.semsportal.com) or in the app.
5. Use the **visitor e-mail and password** when adding this integration in Home Assistant.

---

## Installation

### Via HACS (recommended)

1. Open HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/prezervos/goodwe-wallbox-sems-home-assistant` as **Integration**
3. Search for **GoodWe SEMS Wallbox** and download it
4. Restart Home Assistant

### Manual

Copy the `custom_components/sems-wallbox/` folder into your HA `config/custom_components/` directory and restart.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for **GoodWe SEMS Wallbox**.

The setup is guided and automatic:

1. Enter your **SEMS Plus / semsportal.com** username and password.
2. The integration queries your account and lists your **plants** (if you have more than one).
3. It then lists the **EV chargers** in the selected plant — pick yours, or confirm the only one detected.
4. If automatic discovery fails (EU gateway unavailable, no EU account), you are prompted to enter the **wallbox serial number** manually.

The discovered **Plant ID** and **product model** are stored automatically — no manual copy-paste from URLs needed for Gen2 (HCA series) chargers.

After setup the integration creates a single device with all entities listed above.

---

## Gen2 / HCA series chargers

Chargers in the HCA product family (e.g. `GW7K-HCA-20`) use the **SEMS Plus EU gateway API** to set the charge power limit instead of the legacy semsportal.com endpoint.

The integration handles this automatically:

- During setup the **Plant ID** is discovered and saved.
- All set-mode / set-power commands are sent exclusively to `eu-gateway.semsportal.com` — the old SetChargeMode endpoint is skipped entirely to avoid device-busy timeouts.
- If the EU gateway is unreachable, the integration falls back to the legacy API (which works for Gen1 chargers without SEMS Plus).

You can review or override the Plant ID and product model at any time via  
**Settings → Devices & Services → GoodWe SEMS Wallbox → Configure**.

---

## Charge mode & power interaction

- **Charge power limit** slider is only active when **Charge mode = Fast**.  
  In PV priority or PV & battery modes the wallbox controls power internally; the slider is shown as *Unavailable*.
- Moving the **Charge power limit** slider always sets the mode to **Fast** and applies the chosen power limit in one API call.

---

## Update interval

The default polling interval is **60 seconds**. You can change it at any time via  
**Settings → Devices & Services → GoodWe SEMS Wallbox → Configure**.

---

## Debugging

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.sems-wallbox: debug
```

---

## Development

```bash
# Install test dependencies
pip install pytest pytest-asyncio requests

# Run tests (must run from repo root, NOT from inside custom_components/)
pytest tests/ -v
```

> On Windows, always run `pytest` from outside the project root to avoid the stdlib `select` module being shadowed by `custom_components/sems-wallbox/select.py`.

---

## Changelog

### 1.3.0
- **Visitor account support**: config flow discovers plants and chargers from EV charger `stationId` — no owner access required
- **Auto-discovery of `productModel`** via `ev-charger/control-item-content-list/{sn}` (EU gateway); manual fallback step shown only if discovery fails
- **Existing entries without `productModel`** auto-recover at startup — fixes R0219 `model_not_supported` errors after upgrade
- **set-config fallback removed** — was returning `success` on anything and masking real failures; set-mode is now the only control path
- **set-mode timeout raised to 90 s** — device can take 60–90 s to respond
- **R0305 `remote_control_fail`** retried 3× with 2 s delay before giving up
- **Number entity grace period (120 s)** — slider holds the new value while the device slowly applies it; confirm poll fires 60 s after set-mode returns
- **Status sensor**: maps gen2 API values (`available`, `charging`, `offline`) alongside legacy `EVDetail_Status_Title_*` strings
- **Workstate sensor**: maps gen2 `workState` values (`available_gun_no_insered`, `available_gun_insered`, `finishing`, …)
- **Power sensor**: returns `0` when `startStatus=False` — `chargePower` from the API is the configured *limit*, not actual draw
- **Switch**: uses `startStatus` boolean for `is_on` instead of `power > 0` (power = limit, not actual consumption)
- **Charging detection** for dynamic polling uses `startStatus` instead of `power > 0`
- 135 unit tests, all passing

### 1.2.0
- **Auto-discovery in config flow**: after login the integration queries the EU gateway and automatically detects your plants and EV chargers — no manual serial number entry needed
- **Gen2 / HCA series**: full support via SEMS Plus EU gateway (`eu-gateway.semsportal.com`)
  - Plant ID auto-detected from account — no URL copy-paste needed
  - Set-mode / set-power sent exclusively to EU gateway (legacy SetChargeMode skipped) to avoid 30 s device-busy timeouts
  - Password encoded correctly as `base64(MD5(password))` per SEMS Plus browser protocol
- Dynamic polling: faster interval while charging, slower while idle
- Options flow: override Plant ID, product model and polling intervals at any time

### 1.1.0
- Full Czech and English entity translations via HA translation system (`_attr_translation_key`)
- Restored `SemsCurrentSensor` (charging current in Amperes)
- `SemsWorkStateSensor` — vehicle connection state (Not Plugged in / Connected / Finished Charging)
- Shared `SemsUpdateCoordinator` — single API poll shared across all platforms
- **Charge power slider disabled** when charge mode ≠ Fast
- Grace period logic in charging switch (130 s) to prevent flickering after ON/OFF commands
- Unit tests: 73 tests covering API, sensors, switch logic

### 1.0.0
- Initial release by [@prezervos](https://github.com/prezervos)

---

## Credits

Based on the original work by [@prezervos](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant),  
which was itself inspired by [@TimSoethout/goodwe-sems-home-assistant](https://github.com/TimSoethout/goodwe-sems-home-assistant).
