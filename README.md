# GoodWe SEMS Wallbox — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Tests](https://github.com/pedrodivisez/goodwe-wallbox-sems-home-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/pedrodivisez/goodwe-wallbox-sems-home-assistant/actions/workflows/tests.yml)
[![Validate](https://github.com/pedrodivisez/goodwe-wallbox-sems-home-assistant/actions/workflows/validate.yml/badge.svg)](https://github.com/pedrodivisez/goodwe-wallbox-sems-home-assistant/actions/workflows/validate.yml)
[![GitHub release](https://img.shields.io/github/release/pedrodivisez/goodwe-wallbox-sems-home-assistant.svg)](https://github.com/pedrodivisez/goodwe-wallbox-sems-home-assistant/releases)

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
- A [SEMS Portal](https://www.semsportal.com) account (visitor account recommended — see below)  
- GoodWe EV Charger (Wallbox) registered in the SEMS Portal

---

## Installation

### Via HACS (recommended)

1. Open HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/pedrodivisez/goodwe-wallbox-sems-home-assistant` as **Integration**
3. Search for **GoodWe SEMS Wallbox** and download it
4. Restart Home Assistant

### Manual

Copy the `custom_components/sems-wallbox/` folder into your HA `config/custom_components/` directory and restart.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for **GoodWe SEMS Wallbox**.

| Field | Description |
|-------|-------------|
| Username (e-mail) | SEMS Portal login |
| Password | SEMS Portal password |
| Wallbox serial number | Found in the SEMS app or on the device label |
| Update interval (seconds) | How often to poll the API (default: 60) |

> **Tip:** Create a **visitor account** in the SEMS Portal app and use that to avoid exposing your main credentials. The visitor account has read+control access to the charger.

After setup the integration creates a single device with all entities listed above.

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
