# Development and validation

Use an isolated Home Assistant instance. The component package is
`custom_components/sems_wallbox`; install or bind that directory into the test
instance's `custom_components` directory. Do not run two HA processes against one
configuration directory. Repository unit tests use lightweight HA stubs and do
not replace testing in a real supported HA runtime.

## Unit checks

```sh
python -m pip install pytest pytest-asyncio requests voluptuous aiomqtt==2.5.1
python -m pytest tests/ -q
```

The CI workflow runs these tests on Python3.12 and3.13. They cover existing API
behavior, saved intent, mode/power preservation, cancellation races, framing,
identity validation, protective Stop, ownership conflict and cloud freshness.

For maintenance of the new modules, Ruff can additionally check unused names and
imports. No formatter is imposed on unrelated legacy files. Text files are UTF-8;
preserve an existing file's newline style to avoid whole-file diffs.

## Real HA runtime checks

Use the Python environment of the development HA installation. These scripts use
temporary configurations, mocked cloud endpoints and a loopback TCP wallbox. They
do not control a physical wallbox or access a production HA configuration.

```sh
python scripts/ha_runtime_smoke.py /absolute/path/to/custom_components
python scripts/ha_native_runtime_smoke.py /absolute/path/to/custom_components
```

The native smoke covers two cloud/TCP cycles, actual HA entity service wiring,
all-mode Start/Stop and power preservation, live power changes without mode changes,
model-derived entity bounds, protection notifications, inherited charging sessions,
reload restoration and rejection of stale cloud data. An error message from the
intentional stale-cloud negative case is expected; the script must end with PASS.

## Physical verification

Physical tests must use an independently bounded recovery process, observe actual
power rather than ACKs/setpoint echoes, isolate any competing power automation,
and restore the current production charging intent afterwards. Keep credentials,
site addresses and experimental firmware/control probes outside this repository.
Historical physical-research scripts are not supported maintenance commands.

Current GW11 HCA evidence covers all-mode Start/Stop, saved power across mode changes,
explicit power increases/reductions and cloud/TCP restoration. It does not establish
identical behavior on GW7/GW22 or autonomous PV-surplus regulation. Recheck physical
behavior when changing wire encoders, timing/recovery semantics or power handling;
documentation/formatting cleanup alone does not justify extra charging cycles.

## Language conventions

Use English for source code, comments, documentation, logs, runtime error messages,
and default UI strings. Keep strings.json and translations/en.json synchronized.
Put Czech UI text only in translations/cs.json; use UTF-8 with Czech diacritics.
Keep machine-facing state values and entity identifiers stable across languages.

Native entities must declare translation keys rather than hardcoded display names.
The native HA runtime smoke checks every registered entity against the English and
Czech catalogs, including mode options and status/transport values. Shared entities
reuse the legacy translation keys; internal option values and unique IDs stay stable.

Service errors use HA exception translation keys at the UI boundary; technical
causes stay in logs. Config selectors use translated options while saved charging
modes remain integers. Native smoke verifies invalid power sends no device write.

## Core-integration-inspired regression coverage

Reference: Home Assistant core `tests/components/peblar` (reviewed 2026-09-17).
Adapt scenarios to verified GoodWe capabilities; do not copy Peblar command semantics.

- `scripts/ha_native_contract_checks.py`, called by the native runtime smoke:
  complete native-entity identity/metadata contract, common device association, units,
  diagnostic/default-disabled choices, translated labels and actual HA icon loading;
  user name retention; unavailable/recovered entities; deduplicated reauthentication
  on rejected credentials without inappropriate transport fallback.
- `tests/test_ui_contracts.py`: icon keys/states match EN/CS entity catalogs;
  measurement icons remain device-class defaults; service-error placeholders match.
- `tests/test_switch.py`: capability-gated cloud controls and no writes during setup.
  Keep the upstream minimum-power compatibility behavior for old entries explicit.
- `tests/test_mode_options.py`: configuration, duplicate/identity protection,
  reconfigure/reauth validation and rejection of Auto without cloud credentials.
- `tests/test_native_fallback.py`: debounce, backoff, completed recovery, command
  races, local failure recovery and cancellation/awaiting of the background task.
- Existing policy/protocol suites and real HA smoke cover mode/power persistence,
  bounds, raising limits again, actual service calls, no uncertain Start replay,
  reload/unload, packet validation, endpoint ownership and diagnostic redaction.

RFID, firmware update and socket-lock tests are not applicable without implemented
GoodWe capabilities. Plug and Charge and energy continuity scenarios become release requirements when
those features are implemented. The current suite must not claim those pending
behaviors are already supported. Latest-intent buffering is covered below; a
physical deferred Stop passed after the shared-session correction, as recorded in
`VALIDATION.md`. Earlier failures remain in `VALIDATION.md`.


Latest-intent follow-up: `tests/test_native_intent.py` covers bounded click bursts,
shared Start/Stop intent, latest power/mode, transport-specific prerequisite order,
expiry, shutdown/external cancellation, failed preparation, uncertain Start and
requests arriving during error reporting. Native runtime smoke also sends actual
HA service calls during a held endpoint transition and verifies that a cancelled
Start never appears on the simulated wire. The fallback suite bounds slow cloud
refreshes within the 60-second trial budget. These tests do not substitute for
physical transport-latency measurements.

`tests/test_native_preflight.py` and fallback regressions cover saved regional
endpoints, read-only probes, expected stale/offline API reports, identity checks,
connection failure/timeouts/cancellation, retry spacing, reauthentication, manual
overrides and charging races. Failed probes must not acquire the control lock or
change transport. Successful probes still require the normal fresh-data trial.

Cumulative storage reads: `tests/test_native_energy.py` validates exact framing,
independent storage integrity, full-width counters, fragmented loopback replies,
idle-only admission and continued status/control after timeout, corruption or
cancellation. The combined energy/native-transport suite passed 106 tests.
The reader was also checked against archived frames and two identical fresh
physical idle snapshots. The optional sensor and session-energy follow-ups add HA-level coverage described
below; these reader tests alone do not establish statistics or device reset behavior.

Transport preference persistence: `test_native_connection_intent.py` covers manual
TCP/cloud round trips, automatic outage hints, failed storage writes, startup
selection, crash-journal recovery and bounded initial cloud reads. The HA native
runtime smoke reloads a manual TCP entry with stale cloud data and verifies local
readiness without control replay; it also reloads automatic fallback, expires the
short startup cloud trial and verifies TCP returns without a new outage debounce.

Optional energy entity: `test_native_energy_polling.py` covers default-off behavior,
idle cadence, session-end reads, control priority, reset/zero pass-through, missing
data, session fences and cancellation. `ha_native_energy_checks.py` enables the
entity through the HA registry, obtains actual loopback storage frames, verifies
units/state class/stable reload identity, invokes HA's reset-detection routine and
checks cloud unavailability. It does not claim a full recorder database migration
or a physical factory-reset test.


### Cloud event regression checks

Run focused checks in the existing development environment:

```sh
python -m pytest -q tests/test_cloud_push.py tests/test_cloud_push_settings.py tests/test_sems_api.py tests/test_observed_state_diagnostics.py
```

These cover event identity, malformed/retained/duplicate messages, burst coalescing,
transport epoch changes, cancellation, reconnection after TCP, optional failure
isolation, regional broker validation, token retry, and diagnostic redaction.
A real broker connection alone does not prove delivery for every Start/Stop.
See `VALIDATION.md` for the bounded physical experiment outcomes.


Adaptive polling checks:

```sh
python -m pytest -q tests/test_cloud_push_polling.py tests/test_cloud_push.py tests/test_observed_state_diagnostics.py
python scripts/ha_cloud_push_polling_smoke.py /workspaces/sems-wallbox/custom_components
```

The smoke test uses real HA scheduling and simulated reports with no wallbox or
cloud network I/O. It verifies qualification, doubled backup interval, immediate
refresh on disconnect, and normal polling restoration. Unit cases additionally
cover silence, malformed/stale/future/repeated reports, failed reads, transitions,
reload initialization, and charging/idle interval changes.


## Test maintenance and scope

Use focused tests for a local change; run the full suite and relevant real-HA
smokes at a release checkpoint or after shared test infrastructure changes.
Do not rerun physical charging merely because a unit test was refactored.

- Number/select behavior: `python -m pytest -q tests/test_number.py tests/test_select.py`.
- MQTT behavior: use the cloud event and adaptive polling commands above.
- Transport/policy changes: select the affected native/policy modules and their
  runtime smoke when HA lifecycle or service wiring changes.
- Release checkpoint: `python -m pytest -q tests` plus applicable runtime smokes.

The number/select cleanup combines repeated command, UI state and reconciliation
assertions into scenario tests. Parameterization retains the original power
inputs and all three charging modes. Independent cancellation, uncertain-write,
protective-stop and race regressions remain separate: similar setup does not
make their failure modes equivalent. Legacy cloud tests remain relevant for
entries that do not use the native transport policy.

Unit stubs and real-HA smoke tests serve different purposes: fast edge-case
coverage versus actual HA service/lifecycle wiring. Neither proves physical
wallbox behavior or live cloud delivery. Keep expected wire values independent
of the implementation under test. Prefer shared helpers for new reusable setup;
do not add more imports from one test module into another. Existing cross-module
fixture imports are a future infrastructure cleanup, not a reason to delete
behavioral coverage.

## Focused lifecycle regression

Run `python scripts/ha_native_runtime_smoke.py /workspaces/sems-wallbox/custom_components --lifecycle`
in the existing development container. This isolates reload/disable/setup overlaps
using loopback TCP and fake cloud only; it does not operate a physical charger.
See VALIDATION.md for the pre-fix failure and assertions.

## Current release checkpoint

See [validation](VALIDATION.md) for tested versions, coverage and remaining limits.
The MQTT reconnect test exercises the actual listener/shared-session broker discovery
with HTTP/MQTT I/O replaced. It checks credentials, subscriptions, transient failure,
polling and cleanup; it does not establish natural expiry at the actual broker.
