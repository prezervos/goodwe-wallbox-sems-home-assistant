# Draft release notes: cloud/native TCP reliability update

UNRELEASED. Proposed changes relative to upstream 2.0.0; no tag or release created.
Version number is not assigned. The installed development manifest still says
2.0.0; choose and apply the release version before publication. These notes cover
the accumulated candidate relative to the recorded upstream baseline
9ce5772195f13cc9c4082a93e09b87213a1e2a6a, not only the latest production deployment.
## Features and behavior

- Optional native Socket A TCP control alongside cloud and the existing Modbus
  connection path. Native TCP and Modbus are different protocols and modes.
- Opt-in automatic cloud-outage fallback: three failed observations and at least
  90 seconds of failure before TCP takeover. Probe cloud reachability before a
  return trial; verify fresh device data within 60 seconds. Initial return attempt
  follows 30 minutes on fallback TCP, with bounded retry/backoff on failed trials.
  Frozen successful cloud reports have a separate freshness timeout; the 90-second
  debounce alone is not a guarantee of takeover within 90 seconds of every fault.
- Explicit TCP selection persists across reload/restart. Automatic fallback does
  not become a permanent manual TCP preference. Authentication failures request
  reauthentication instead of being treated as permission to switch transports.
- Visible transition states and latest-intent handling for core controls during
  handovers. Superseded undelivered Start requests are cancelled; already submitted
  device operations are drained and reconciled without blind replay.
- Durable requested charging power in native-enabled entries, independently of
  mode restoration. Legacy cloud-only/Modbus entries enable the shared saved
  mode/power policy through the restoration option. Enabling mode
  restoration captures the current reported mode on final configuration save.
  A new HA mode selection updates saved intent; external device reports do not
  overwrite it. The stored mode is enforced before HA Start, not immediately at
  boot. Reload and restart preserve saved intent.
- MQTT event hints share authentication/session handling. HTTP polling remains
  the source of verified observations unless a fresh telemetry stream qualifies
  a temporary extended polling interval. MQTT connection alone never qualifies.
- Clearer configuration navigation, translated errors/states and entity labels.
  Charging mode is a primary control. Requested, reported and measured power have
  distinct meanings; the reported limit is diagnostic.

## Correctness fixes

- Cancel cloud Start while it is waiting for the executor/shared HTTP lock when
  Stop, shutdown or newer mode intent supersedes it.
- Clean up unpublished native setup resources even if endpoint restoration fails
  or cleanup is repeatedly cancelled; retain the recovery journal.
- Reject stale TCP observations across transport handovers before HA publication.
- Release supervision after verified Fast completion; retain protection during
  EV/PV pauses and adopted cloud-originated PV sessions, including external PV reports.
- Disable automatic pymodbus write replay after lost acknowledgements. Verify the
  enrolled serial on the same connection before writing; reject missing or wrong
  device identity on observations and reject use of a closed client.
- Report rejected cloud/Modbus settings as translated HA service errors; clear
  optimistic values and reconcile state instead of returning false success.
- Preserve absent/invalid optional settings as unknown; do not invent Off or zero.
- Prefer current vehicle observations over stale last-session completion records.
  Preserve known model/firmware metadata and the cloud session-energy entity.
- Correct the native HA category contract test; expand real-HA/loopback regression
  coverage and complete English/Czech/German/Spanish translation-key coverage.

## Upgrade and compatibility notes

- Back up HA before upgrading. Current upstream baseline and this package both
  use sems_wallbox. Historical sems-wallbox installations are a separate migration;
  automatic migration from that historical domain is not claimed.
- Existing unique IDs were preserved for the reviewed baseline. The production
  migration verified 28 registry identities. Other registries, custom dashboards
  and consumers still need their own upgrade check.
- User-assigned entity names are not rewritten. Default translated labels,
  icons and categories can change; category changes can affect auto-generated
  dashboards and device/area service targeting.
- Power values use kW; energy uses kWh. Do not interpret unavailable/unknown as zero.
  Requested power is saved HA intent, not a live measurement or proof of device
  enforcement. Cloud power writes select Fast as upstream did; native writes
  preserve mode. Cloud/native requested-power controls use a box instead of a slider; the Modbus
  control remains a slider.
- Suspended EV/EVSE states do not imply charging completion. No universal
  charged-to-finished_charging alias is introduced. Audit state/attribute-based
  templates: combined telemetry only exposes attributes that were actually reported.
- Session energy uses the same entity identity and kWh unit through cloud and
  verified HCA native TCP. The device resets the native session counter after
  Stop. Physical handover and Recorder checks found no double counting in the
  tested session. Native cumulative energy remains separate and opt-in; neither
  lifetime energy nor an HA power integral substitutes for session energy.
- Unsupported extended settings remain unavailable on TCP. Minimum-power state
  is visible in all modes; native writes are validated only while idle, cloud
  PV-mode writes are validated, and the ineffective cloud Fast setter is guarded.
- SolarGo Auto start was physically demonstrated, but no portable verified
  cloud/native setter plus readback is established for the tested original HCA.
  Accepted cloud writes alone do not establish hardware support.
- Dependencies currently declare pymodbus>=3.0.0 and aiomqtt==2.5.1. Actual Modbus
  wire regression used pymodbus 3.15.0; the broad lower bound is not evidence that
  every allowed dependency version was tested.

## Validation and remaining limits

980 unit tests and 13 isolated real Home Assistant smoke scenarios passed on
HA 2026.9.2 / Python 3.14.5. See [validation](VALIDATION.md) for exact scope and
limitations. CI also runs Python 3.12/3.13 and HACS/hassfest checks.

Physical native evidence covers original GW11 HCA. Extended TCP settings, Auto start
API support, original-HCA load-management effect, natural token expiry/MQTT
reauthorization and other hardware remain unverified. Changes to minimum-power
settings require confirmed idle on the tested original HCA; an active-write bypass
is not implemented. Accepted API writes alone do not prove hardware support.

The manifest version remains 2.0.0 pending release-version assignment. This pull
request does not create a release or claim automatic migration from sems-wallbox.
