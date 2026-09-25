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
GoodWe capabilities. Native TCP Auto start has protocol, entity and concurrency coverage on original
HCA, including changes while charging. Cloud Auto start remains capability-gated;
do not infer its support from an accepted API write or from Modbus controls. Session
energy continuity and optional native lifetime reads have separate coverage below. Latest-intent buffering is covered below; a
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
python scripts/ha_cloud_push_polling_smoke.py /workspaces/sems-wallbox/custom_components --cooldown-only
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

## Dependency compatibility

The manifest pins `pymodbus==3.13.1`, matching the built-in Modbus integration
in the tested Home Assistant 2026.9.2 runtime. Our client uses `device_id=`,
introduced in pymodbus 3.10; the former `>=3.0.0` requirement admitted incompatible
older clients and untested future API changes. Pymodbus minor versions can change
its API. When updating this pin, check the target HA Modbus requirement too and
run the existing `scripts/ha_audit_regressions.py` wire checks with the actual
package. They verify identity reads, writes, uncertain Start without replay and
closed-client rejection against a loopback server. Do not replace these checks
with mocks of pymodbus's call signatures.

Version 3.13.1 passed the focused wire checks in HA 2026.9.2; 3.15.0 passed the
previous release checkpoint. No physical Modbus hardware is claimed as tested.
The MQTT dependency remains pinned to the tested `aiomqtt==2.5.1`.
`requests` is supplied explicitly by Home Assistant core (2.34.2 in the tested
runtime); a separate integration pin would unnecessarily duplicate that contract.

## Transient native idle reports

NativeModeAdapter rechecks only stopped-state reports with zero measured power
and residual phase current. Two extra status reads, one second apart and bounded
by two seconds each, preserve the existing operation deadline/cancellation fence.
The same check applies to policy verification and the final adapter read before
Start. Active charging, nonzero power or a pending supervised Start are not retried.
Only reads repeat; Start delivery and protective semantics are unchanged.

Focused coverage in test_native_transport exercises the observed0kW/2.7A case
settling in both preparation stages, persistent contradictions, active charging,
nonzero power, pending Start, read timeout and Stop invalidation during the delay.
Existing loopback transport and handover tests retain wire-level coverage.

## Cloud session diagnostics

The cached diagnostic export includes login_attempts, successful_logins,
session_recovery_attempts and last_login_age_seconds. Login attempts count logical
login cycles, including an eligible endpoint fallback as one cycle, not HTTP calls.
Recovery attempts count explicit C0602 responses that initiate bounded renewal;
they do not distinguish natural expiry from another client invalidating the session.
MQTT subscription_count increments only after subscriptions succeed;
last_subscription_age_seconds describes that event, not message freshness.

Counters are in-memory, scoped to the API/listener instance and reset on recreation.
Export contains no token, token hash, username, broker password or login response.
Reading diagnostics never forces login, MQTT reconnect or cloud polling. A successful
second login does not by itself prove that the first session became invalid. Compare
recovery/login deltas and subsequent successful normal polling; MQTT may remain
connected across HTTP session renewal and must still be assessed independently.

## Interpreting live recovery evidence

The 2026-09-23 live checks verified forced HTTP-token rejection/recovery, MQTT
network-outage reconnection, and actual charging events after resubscription.
See VALIDATION.md for outcomes and limitations. Do not repeat physical charging
for documentation-only edits. Natural expiry, continuous MQTT telemetry, exact
HTTP request cadence and a live qualified-stream fallback remain separate checks.

Network fault injection belongs in disposable development tooling, not the
integration. Scope it to the broker in the development network namespace, verify
HTTP API addresses are unaffected, and provide independent timed cleanup. Physical
Start/Stop testing additionally needs a bounded Stop guard and ownership restoration.
A watchdog command must be included when interpreting event/command counts.

## HTTP request cadence without physical charging

The existing ha_cloud_push_polling_smoke.py also exercises NativeCoordinator and
SemsApi with real HA scheduling at60s idle/30s charging. Only the requests session
network boundary is replaced; responses change simulated state without any Start,
Stop or setting operation. Unexpected methods/endpoints fail the test. Credentials
are fictional and cached to isolate steady-state polling from authentication tests.

It counts telemetry and configuration HTTP calls separately, checks idle/charging
spacing, coalesces20distinct MQTT hints to one read, rejects a repeated event ID,
and confirms the normal scheduler continues after push loss. HA aligns scheduled
callbacks to clock boundaries, so assertions allow its subsecond rounding and a
small execution margin. Run through a non-loopback network guard in development.
This complements, rather than repeats, the existing simulated adaptive-polling
qualification check. It is not a live GoodWe traffic measurement.

## MQTT and delayed HTTP response races

The existing cloud-push tests cover pending hints and listener lifecycle changes.
The in-flight refresh scenario additionally gates an awaited read:20new hints
share that task, completion allows future hints, and unload drains/cancels it.
A replacement listener must operate without reviving the old instance.

For a focused real-HA executor/publication check, run:

```sh
python scripts/ha_cloud_push_polling_smoke.py /workspaces/sems-wallbox/custom_components --races-only
```

It holds a cloud response in an executor until a newer TCP observation has been
published, then verifies epoch rejection prevents data replacement. A stale refresh
may mark availability failed until the next current observation; the test does not
promise continuous availability through a handover. Existing configuration-response
and native lifecycle tests cover their separate races; do not duplicate them here.

## Shared cloud cooldown

`CloudRequestGate` serializes HTTP dispatch across a SemsApi instance and its v3
observation reader. This includes settings, controls, MQTT credential discovery
and login. HTTP429 honors a numeric/date Retry-After, or uses 30/60/120/240/300s
backoff when missing/invalid. HTTP503 honors valid Retry-After. A successful HTTP
response resets the headerless backoff. Other HTTP failures retain their existing
classification. Deadlines use monotonic time; an expired HTTP date is invalid.

No worker sleeps through a cooldown and no command is queued or replayed by the
gate. A typed error reaches the translated service boundary. A rejected optimistic
setting is cleared. Existing opt-in TCP preflight fallback remains available;
uncertain cloud command delivery does not justify replay. Cancellation and
operation budgets also apply while waiting for the HTTP serialization lock.

The gate is per configured API instance, not a cross-process account lock. It does
not coordinate other HA instances, SolarGo or the official web application. A new
API instance starts with no remembered server cooldown. Login-specific rejection
and fallback rules remain separate, but known throttling blocks HTTP dispatch.

`tests/test_cloud_rate_limit.py` verifies request counts, deadlines, concurrent
callers and shared web/telemetry/login blocking with mocked HTTP only. Existing
command, fallback and policy tests cover no replay and localized entity errors.

Run the integrated recovery smoke separately (real HA, mocked HTTP/MQTT and a
loopback native peer; no physical control):

```sh
python scripts/ha_cloud_push_polling_smoke.py /absolute/path/to/custom_components --cooldown-only
```

It verifies cloud-only recovery and automatic TCP fallback/return together with
MQTT reconnection, polling and freshness guards. Scheduler/supervisor delays are
shortened explicitly; Retry-After uses real elapsed time. Existing cadence and
race smoke modes remain separate so targeted checks do not rerun long waits.


## Test maintenance contracts

Unit tests use central lightweight HA stubs from `tests/conftest.py`; real HA
scripts run separately and must not import that conftest. Unexpected requests HTTP
calls fail unit tests locally. Native protocol peers use loopback sockets only.

The normal cloud control fixtures advertise timestamped observation, matching
`SemsApi`. Keep SEMS+ configured power separate from the older telemetry allocation;
matching values at every endpoint hide regressions. The Fast-preparation matrix
labels its older reader explicitly as `legacy_cloud`, alongside cloud and Modbus.
Compatibility tests remain relevant while the corresponding production path exists.

Run the additional real-HA settings race scenario with:

```sh
python scripts/ha_native_runtime_smoke.py /absolute/path/to/custom_components --cloud-settings-only
```

It uses real services, executor work and background configuration refreshes. An
old read is held across a power write, then released; it must not republish the
old limit. Configuration/telemetry disagreement, failed reads, unknown state and
recovery are checked on registered HA entities. All barriers have bounded waits
and release in cleanup. No physical charging is involved.

HTTP fixtures should model status, JSON and `raise_for_status`, preferably with
`requests.Response`. Assert the specific expected exception and renewed token,
not merely that something failed. Expected model ranges must be independent
constants, not calls to the implementation being tested. Preserve distinct missing,
zero, stale, superseded and transport-epoch cases when consolidating scenarios.

For the complete current real-HA command list, follow `.github/workflows/tests.yml`.
Test results prove simulated contracts, not physical behavior on untested Modbus
hardware or unobserved firmware versions.


## Bounded control readback (3.0.4 prereleases)

Accepted HA controls initiate read-only confirmation. Cloud attempts target 5, 10,
20, 35 and 60 seconds after command completion; Modbus targets five-second reads
for up to 60 seconds. Slow responses, HA refresh coalescing and API recovery may
postpone a read; missed slots are skipped, never replayed as a burst. Native TCP
retains its existing immediate updates and 2/5-second polling.

Each normalized setting keeps only its latest requested value. Start and Stop
share one key. A later command invalidates an earlier command's late completion.
Staged Fast-mode power preferences do not initiate readback until actually written.
Only successful coordinator/configuration reads can confirm a request; optimistic
entity presentation and a request ACK cannot. Power confirmation uses the reported
configured limit, not measured charging power. Cloud settings readback uses SEMS+
configuration rather than the potentially stale limit in v3 telemetry.

Normal polling and MQTT-triggered reads can satisfy the same pending requests.
Cloud settings temporarily bypass their five-minute cache interval when needed.
Read failures stop accelerated reads and retain existing authentication/rate-limit
recovery. Transport changes and unload cancel pending confirmation. Expiry only
records `unconfirmed` in diagnostics: it does not repeat writes, stop charging,
change entity telemetry or create persistent notifications. A PV waiting state
without a verified enabled-session indicator remains unconfirmed; it is not
misrepresented as successful charging.

Focused scenarios: `tests/test_write_confirmation.py`. Physical validation of
Modbus readback timing still requires a supported Modbus wallbox.

Beta2 configures the shared HA request-refresh debouncer with a five-second
cooldown. An actual second-timer regression guards against the default ten-second
cooldown; a mutation check proves that test fails with the old setting. The cloud
SEMS+ coordinator confirms Start from session work status 6, and Stop from known
terminal statuses 8/10. Missing or other statuses do not confirm Stop merely
because detail says available. Native v3 and Modbus keep separate interpretations.
A coordinator read begun before the accepted request cannot confirm it. This
fences local in-flight reads; it does not prove the server has no internal cache.

A cloud setting timeout retains the service error and arms read-only reconciliation
for the latest setting. Outcomes are pending_after_timeout, confirmed_after_timeout
or unconfirmed_after_timeout; ordinary read failure/cancellation outcomes still
apply. A matching report confirms the requested value, not which request caused
it. No write replay or automatic service success is inferred. Start/Stop and
Modbus timeout handling are not changed by this setting-only reconciliation.
