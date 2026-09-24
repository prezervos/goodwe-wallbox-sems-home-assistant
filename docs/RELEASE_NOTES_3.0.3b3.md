This prerelease fixes the cloud Import current limit error reported in #21 when the device reports 63 A but Home Assistant previously allowed only 0–32 A.

## Changes
- Use the SEMS+ device-specific current-limit range. Missing/null bounds in a successful metadata response default to 0–2000 A, matching the official client.
- Preserve reported values and existing entity identities. Support two-decimal cloud inputs without rounding or clamping.
- Recheck metadata and device identity before writes. Invalid or contradictory data prevents changes; an API acknowledgement does not replace reported state.
- Update translated errors and explain the distinction between household current limits and vehicle charging power.
- Includes the previous beta's Modbus setup fix and diagnostic trace. The separate Modbus charging-interruption issue remains unresolved.

## Please verify (no charging or setting changes required)
1. Install 3.0.3b3 through HACS with prereleases enabled, then restart Home Assistant.
2. Open the cloud integration: Import current limit should show the reported 63 A without the previous range error.
3. Reload the integration and confirm the value remains and the error does not return.
4. Report the displayed value and minimum/maximum. If unavailable or an error remains, download integration diagnostics and share them after checking for private information.

Do not change the household current limit just to test this fix. Model-specific physical enforcement is not yet verified. Home Assistant 2026.9.2 or newer is required. This is a prerelease; 3.0.2 remains the latest stable release.

## Validation
The exact prerelease tree passed 1,304 automated tests, real Home Assistant configuration/entity smoke checks and Ruff F checks. These checks use simulated device/API responses; reporter hardware confirmation is pending.
