# 3.0.4b3 — Consistent Start/Stop feedback

## Fixed

- **Charging switch feedback with a preferred mode:** cloud and Modbus Start/Stop now retain the accepted control intent while telemetry catches up, including when the saved charging-mode policy handles the command. That path previously bypassed the existing pending-command display and could briefly show Off after an accepted Start.
- **Latest request wins:** a delayed completion of an older policy Start cannot overwrite a newer Stop in the switch presentation.
- **Modbus cached state:** a cached pre-Start idle snapshot no longer immediately cancels newly acknowledged policy intent. Fresh terminal reports and the existing pending timeout still clear it.
- **Regression coverage:** delayed cloud sessions, Modbus handshake/terminal reports, expiry, rejected/uncertain commands and overlapping Start/Stop. Stabilize a Windows-sensitive deadline test while retaining real async cancellation.

The Charging switch represents control/session state; measured power and charging activity remain independently reported. No additional Start writes are sent and telemetry is not fabricated. Native TCP behavior, entity identities, saved preferences and normal polling settings are unchanged. Includes the readback and uncertain-setting fixes from b1/b2.

## Please test

Enable prereleases in HACS, install **3.0.4b3**, and restart Home Assistant (minimum **2026.9.2**).

1. With your usual preferred-mode settings, issue **Start once**, first using Modbus and then cloud. Check whether the Charging switch stays On through the initial handshake instead of briefly bouncing Off. Compare it with the actual power/station status; an accepted Start does not itself prove energy flow.
2. Verify **Stop**, including a short Start-to-Stop sequence when safe. A delayed Start response must not turn the switch back On after a newer Stop.
3. If Start displays an error but charging begins afterwards, capture debug logs from before the request through the next minute, with the exact error and timestamps. Check actual state before retrying; do not repeatedly click Start. Redact credentials/tokens before attaching logs.

## Known limits

This beta does **not** claim to fix GoodWe cloud response latency. The reported first cloud request took about 14 seconds for acknowledgement, and positive power first appeared about 39 seconds after the request. Logs do not establish the precise physical start time. The separate unlogged service-error-then-charging case remains unconfirmed.

Hardware confirmation is still requested, especially for Modbus. **3.0.3 remains the stable release** and rollback option. No production deployment accompanies this prerelease.

Thanks to @GregoryDC for the logs and hardware testing.

# 3.0.4b2 — Readback timing and uncertain cloud writes

This beta follows the hardware feedback for 3.0.4b1 in #21. It fixes a remaining readback delay and improves handling of contradictory cloud responses and uncertain setting writes.

## Fixed

- **Repeated readback cadence:** configure HA's shared refresh debouncer for five seconds instead of its default ten. Successive unresolved checks no longer acquire an extra ten-second delay. Network/response time still adds latency; normal configured polling intervals remain unchanged.
- **Cloud Start/Stop confirmation:** the SEMS+ coordinator uses its newly fetched charging-session status, consistent with the source used by the Charging switch. An unreliable detail response saying `available` cannot prematurely confirm Stop or prevent confirmation of an active session. Missing or ambiguous session data stays unconfirmed. Native TCP/v3 keeps its own state interpretation.
- **Uncertain cloud settings:** a setting request that times out remains a service error with a clearer translated message: the change may already have applied. Bounded read-only checks can subsequently record `confirmed_after_timeout` or `unconfirmed_after_timeout` in diagnostics. This does not resend the write, fabricate telemetry, or turn a timeout into an acknowledged success. Superseding commands, cancellation, authentication recovery and transport changes remain protected.
- **Timeout logs:** report actual elapsed set-mode response time rather than a hard-coded 90 seconds when a shorter operation budget applied.

## Validation

Regression coverage now exercises two successive unresolved checks through real HA timers/debouncing, contradictory SEMS+ detail/session reports, old in-flight reads, uncertain setting readback, cancellation and latest-command handling. A mutation check restored the old ten-second debounce and confirmed that the new second-read test detects it. No physical charging or production deployment was performed for beta2.

## Please test

Install **3.0.4b2** with prereleases enabled in HACS and restart Home Assistant. The minimum remains **2026.9.2**; entity identities, saved preferences and normal polling options are unchanged.

1. **Modbus:** issue Start once and record the delay until the Charging switch/station status reflect charging. If handshaking lasts through several checks, include the debug log so we can verify consecutive read intervals. Then verify Stop.
2. **Cloud:** verify Start and Stop again. If the detail API still says `available`, downloaded diagnostics should nevertheless show correct confirmation based on the current session report.
3. **Cloud power limit:** during a suitable session, change the limit once. If the cloud times out, check the reported limit before retrying. Send the timestamp, requested/reported limit, service message and diagnostics after readback completes (up to one minute after the request finishes). A timeout may still occur; this beta improves reconciliation and explains the uncertainty rather than promising faster GoodWe processing.

Please review attachments for private information before posting. Stable **3.0.3** remains available for rollback. This update does not claim to restore SEMS connectivity while Modbus owns the charger or resolve every firmware-level interruption.

Thanks to @GregoryDC for the logs and continued hardware testing.

# 3.0.4b1 — Faster control feedback (prerelease)

This prerelease shortens the delay before controls reflect independently reported wallbox state. It targets the delayed Modbus Start feedback reported in #21; hardware confirmation is still requested.

## Changes

- After an accepted Modbus control, read back every five seconds for up to one minute, stopping earlier when the requested state is confirmed or the device reports rejection.
- After an accepted cloud control, use progressively spaced readback at target offsets of 5, 10, 20, 35 and 60 seconds. Existing reads and in-flight requests are reused; response latency, request throttling and HA scheduling can delay these targets.
- Track only the latest request per setting. Start and Stop supersede each other. No control writes are replayed by this readback mechanism.
- Confirm configuration against the appropriate settings source, not measured charging watts or an optimistic UI state. Preserve unknown values when data is missing.
- Stop accelerated reads on read failure, transport change or unload. Normal polling and existing recovery remain in charge. Expiry creates no persistent notification and does not stop charging.
- Add credential-free confirmation outcomes to downloaded diagnostics. Native Socket A TCP retains its existing immediate reporting and short polling cadence.
- Audit and consolidate regression tests, strengthen stale-read and lifecycle coverage, and add real-HA timer/service checks.

## Validation

The full offline suite passed 1,510 tests. Final refinements passed 459 focused tests, including 30 confirmation cases. Six affected real-HA smoke runs passed in Home Assistant 2026.9.2, covering cloud/Modbus timers, latest-command handling, cloud and native controls, settings, handover and lifecycle. Hardware/API responses in these runs are simulated; no physical charging was performed for this change.

## Please test (#21)

1. Install **3.0.4b1** through HACS with prereleases enabled, then restart Home Assistant. Minimum HA version remains **2026.9.2**; existing entity IDs, preferences and normal polling settings are preserved.
2. In Modbus mode, keep your usual idle polling interval and issue Start once. Record when the switch and station status leave Handshaking and report charging. Do not repeatedly press Start; observe actual charging independently.
3. Check an explicit power-limit or charge-mode change and one Stop when appropriate. Compare the reported configuration with the requested setting; actual consumption need not equal the power limit. Do not change installation/current-protection settings for this test.
4. If possible, repeat through cloud. Report the transport, timing, requested/reported values and any charging interruption. Cloud latency may still exceed five seconds.
5. If feedback is still delayed or a change remains unconfirmed, download the integration diagnostics after the event and provide its timestamp and the relevant log excerpt. The new `write_confirmation` section records the outcome. Review diagnostic/log attachments for private information before posting.

This prerelease does not claim a general fix for firmware-level Modbus charging interruptions, restore SEMS cloud visibility while Modbus owns the charger, or prove every model's timing. You can return to stable **3.0.3** if needed.

Thanks to @GregoryDC for the detailed report and continued hardware testing.

# 3.0.3 — Cloud state, power controls and TCP reliability

This maintenance release fixes cloud status and authentication inconsistencies, restores Fast-mode selection from PV modes, and improves native TCP Stop verification. It includes the fixes previously tested in 3.0.3b1–b3.

## Fixed

- **Cloud authentication (#21):** timestamped telemetry now shares the existing SEMS+ web session instead of performing a separate Android login. Session renewal is bounded and a telemetry-only rejection does not repeatedly invalidate working controls.
- **Vehicle connection state (#21):** prefer the verified SEMS+ connection flag over contradictory “not plugged in” text. Preserve explicit completion reports; suspended sessions and zero power alone do not imply completion.
- **Fast mode from PV modes (#21):** cloud-only and Modbus users can save a valid power preference while in PV mode without sending a device write. Selecting Fast applies and verifies the saved limit. Invalid initial zero-limit reports no longer prevent setup.
- **Household import-current limits (#21):** use device-provided cloud ranges and the documented Modbus range instead of an assumed 32 A maximum. Preserve reported values such as 63 A without clamping or changing the installation setting on load.
- **Cloud reported power limit:** native-capable entries read the diagnostic limit from verified SEMS+ configuration rather than the V3 field that could retain an older value. Related attributes use the same source; missing or invalid readback stays unknown. TCP readback is unchanged.
- **Native TCP Stop:** actively verify stopped telemetry and handle a queued status-query timeout only when a newer valid report arrives on the same live connection. Closed or stale sessions cannot falsely confirm success.
- **Charging activity:** the optional cloud/native activity sensor reflects valid measured energy flow. Zero load is Off; missing, stale or contradictory data is unknown. Charging-session controls retain their separate safety checks.

## Diagnostics and documentation

- Add bounded Modbus connection/read/write tracing to downloaded diagnostics without extra polling or credentials.
- Add passive native fault-detail diagnostics with session isolation, unknown-bit reporting and explicit limits on firmware-label coverage.
- Expand regression coverage, physical validation notes and matching English, Czech, German and Spanish error translations.

## Upgrade

Requires **Home Assistant 2026.9.2 or newer**, unchanged from 3.0.2. Restart Home Assistant after updating. Existing entity identities, units and saved preferences are preserved. Native TCP and automatic fallback remain opt-in.

## Validation and known limits

Physical original-GW11K-HCA validation covered cable connection changes, cloud/native short charging sessions, live power increases/reductions and verified Stops. Cloud diagnostic limit readback was also checked without charging. Requested power is a ceiling, not an exact consumption target.

**The separately reported Modbus charging interruption is not claimed fixed.** The reporter's model/firmware and Australian cloud account still require confirmation. This update does not add speculative EMS takeover writes or automatic charging retries. Other-model native fault mappings, natural token expiry and continuous MQTT telemetry remain outside the verified scope.

Thanks to @GregoryDC for the detailed reports and beta validation in #21, and to all previous contributors.

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
