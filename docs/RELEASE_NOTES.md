# 3.0.2 — Login compatibility, Modbus fixes and TCP Auto start

This update fixes cloud login compatibility and Modbus setup, removes an unsolicited Modbus Stop, and completes verified Auto start control over native TCP for original HCA wallboxes. Existing entity identities, units and saved settings are preserved.

## Fixed

- **Cloud authentication (#21):** prefer the original SEMS+ web login, with one Common/CrossLogin alternate attempt for eligible endpoint/protocol failures, including an explicit success response without a usable token. Keep endpoint-specific request signatures and regional routing. Explicit credential rejection and throttling do not trigger repeated alternate logins; shared session, deadline and cooldown protections remain in place.
- **Modbus setup and reconfiguration (#21):** fix Home Assistant form serialization, normalize host input in the flow handler, and reject blank hosts before attempting a connection.
- **Unsolicited Modbus Stop (#16):** remove the timer that could send Stop after contradictory charging/vehicle-connection reports. Polling is read-only; explicit Start/Stop controls remain available. This does not claim to resolve every firmware-related interruption when a Modbus client connects.

## Completed: native TCP Auto start

- Read and change Auto start on verified original HCA hardware, including while charging. Preserve the existing Plug & Charge entity identity.
- Confirm changes by reading configuration back from the wallbox. Failed or uncertain writes never become a falsely confirmed On/Off state and are not blindly replayed.
- Reject enabling Auto start when a charging schedule is active; disabling and already-satisfied requests remain possible. Detect unexpected schedule changes during confirmation.
- Serialize configuration and cumulative-energy reads, reject stale/late responses, and respect superseding Stop requests before an Auto start write is sent. Cumulative-energy reads retain their idle-only restriction.
- Keep capability-based cloud support for other models. On the tested original HCA, Auto start is usable over native TCP and unavailable in cloud mode; accepted cloud API writes did not establish physical support.

## Diagnostics and translations

- Add credential-free login failure metadata (endpoint, response shape, HTTP/business result and token presence) without logging token values or credentials through these diagnostics.
- Include matching English, Czech, German and Spanish errors for Auto start verification and schedule conflicts.
- Document native TCP limitations, observed behavior and regression coverage.

## Upgrade and validation

Home Assistant **2026.9.2 or newer** is required, unchanged from 3.0.1. Restart Home Assistant after updating. Native TCP remains opt-in; no transport or Auto start setting is enabled automatically by this update.

Independent Astra review found no unresolved runtime blocker. Offline regression and real-HA validation results are recorded in [VALIDATION.md](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/blob/master/docs/VALIDATION.md). Physical Auto start validation covers the original GW11 HCA; other models/firmware are not implied. Authentication and regional reads were confirmed with the issue #21 reporter's Australian account; MQTT connection alone does not establish full telemetry coverage.

Thanks to [@GregoryDC](https://github.com/GregoryDC) for the diagnostic logs and Australian-account verification, and to all previous integration contributors.

# 3.0.1 — Cloud compatibility and reliable controls

Maintenance update to 3.0.0. Existing entity IDs, saved settings, units and native
TCP/cloud selection are preserved. No production deployment is implied by these
release-preparation files.

## Upgrade requirement

**Home Assistant 2026.9.2 or newer is required.** HACS now enforces the tested
minimum. Upgrade Home Assistant first if needed. The Modbus dependency is pinned
to `pymodbus==3.13.1`, matching the checked HA 2026.9.x constraints; MQTT remains
on `aiomqtt==2.5.1`. Earlier HA/dependency combinations are not supported by this
update. Restart Home Assistant after updating the integration.

## Fixed

- Use one Mozilla-format User-Agent for all GoodWe HTTP login, telemetry, control
  and MQTT configuration discovery requests ([#19](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/issues/19)).
  Version 3.0.0 already fixed the original SEMS+ fallback login; this update makes
  the header consistent on the other HTTP paths. It does not establish a fix for
  every intermittent disconnect or timeout. Token client identities are unchanged.
- Respect shared HTTP 429 cooldowns and HTTP 503 responses with Retry-After across
  cloud operations. Surface a translated retry-later error instead of hammering
  the service or replaying uncertain controls.
- Coalesce repeated Start requests without losing a new Start after Stop. Treat
  freshly confirmed charging in the requested mode/power as already fulfilled.
  Uncertain or mismatched charging reports retain their existing safeguards.
- Preserve the latest mode/power choice when an older Start preparation is
  superseded before delivery. Keep a newer explicit Stop after an earlier command
  fails, without replaying the failed command or another uncertain Start.
- Recheck brief contradictory native idle reports with bounded status reads.
  Only reads repeat; a persistent contradiction still refuses Start.
- Require independent CP and measured-power evidence before treating a Modbus
  wallbox as already charging; a stale charging status alone is insufficient.
- Restore the displayed setting after failed/cancelled writes without overwriting
  newer user choices or fresh reports. Pending mode selections no longer replace
  reported coordinator data. Overlapping mode/power writes are covered.
- Remove persistent notifications for routine deferred-control failures and
  fallback progress. Diagnostics/logs retain failure details. Protective alerts
  for failed safety stops remain enabled.
- Clarify refused-Start and rate-limit messages, with matching English, Czech,
  German and Spanish translations.

## Diagnostics and documentation

- Add credential-free login, session-recovery and MQTT subscription counters.
  These counters make no extra cloud requests and are not proof of event delivery.
- Explain how scheduled polling, MQTT hints, user commands and configuration
  refreshes affect HTTP traffic. HTTP polling remains necessary on hardware where
  only charging-event hints have been observed.
- Expand regression coverage for control ordering, rollback, request cooldowns,
  native idle reconciliation and MQTT/HTTP lifecycle races.

## Validation and limits

Validation passed on Home Assistant 2026.9.2 / Python 3.14.5 with pymodbus 3.13.1:
**1,079 unit/regression tests and 14 isolated real-HA smoke invocations**. Independent
Astra review found one additional cooldown-message gap, which was fixed and
rechecked with regression tests. No unresolved blocker remained in the reviewed
scope. See `docs/VALIDATION.md` for limits; these checks do not establish physical
behavior on every model or firmware. GitHub CI/HACS/hassfest for the proposed
release commit still need to run before publication.

This update adds no new native TCP mappings for extended settings and no portable
cloud/native Auto start implementation. Natural token expiry and continuous MQTT
telemetry coverage remain unverified. Existing hardware limitations still apply.

## Thanks

Thanks to [@pedrodivisez](https://github.com/pedrodivisez) for the independent
User-Agent reproduction and earlier wallbox work, and to all previous integration
contributors. The full historical credits remain in the README and 3.0.0 notes.

# 3.0.0 — Native TCP control and cloud reliability

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
- Release 3.0.0 declared pymodbus>=3.0.0 and aiomqtt==2.5.1. Actual Modbus
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

The integration version is 3.0.0. Automatic migration from the historical
sems-wallbox domain is not provided.

### Acknowledgements

Version 3.0 builds on substantial work from previous wallbox releases. Special
thanks to [@pedrodivisez](https://github.com/pedrodivisez) for charging controls,
Gen2/HCA SEMS+, Modbus, translations, documentation and tests, and to
[@frittefrax](https://github.com/frittefrax) for vehicle state, configuration,
manifest and CI improvements. Thanks to [@prezervos](https://github.com/prezervos)
for the wallbox adaptation, maintenance and this release's native TCP work, and
[@TimSoethout](https://github.com/TimSoethout) and the original
[SEMS integration contributors](https://github.com/TimSoethout/goodwe-sems-home-assistant/graphs/contributors)
for the inherited foundation. See the
[full contributor history](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/graphs/contributors)
and [README Credits](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant#credits).
