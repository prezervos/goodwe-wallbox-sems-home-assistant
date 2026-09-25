# Development backlog

Release 3.0.3 packages the fixes summarized in [RELEASE_NOTES.md](RELEASE_NOTES.md).
The sections below retain the development/validation history, including statements
that a change had not yet been released at the time of its experiment. They are
not claims that every historical investigation remains open. Remaining hardware
limits are listed under Remaining investigation and in the current release notes.

## 3.0.4b1 candidate: bounded confirmation after controls

- Cloud: progressively spaced readback (target offsets 5/10/20/35/60 seconds).
- Modbus: five-second readback for at most one minute after accepted controls.
- Native TCP keeps its existing immediate reporting and 2/5-second polling.
- One latest target per setting; Start/Stop supersede each other. Never replay a
  write, fabricate telemetry, or notify persistently on confirmation expiry.
- Cloud configuration uses its own SEMS+ readback, including the reported power
  limit; ordinary telemetry cannot substitute for configuration confirmation.
- Failed reads yield to existing recovery; handover and unload invalidate timers.
- Diagnostics expose pending/confirmed/unconfirmed/failure outcomes.

Validation: 1,510 tests passed in the full offline suite on HP840. After the final
read-source/cancellation refinements, 459 focused tests passed, including 30 new
confirmation cases. Ruff F checks and whitespace checks passed. After restoring
the existing Docker runtime, six real-HA smoke runs passed: audit regressions
(including actual cloud/Modbus timers and latest-command confirmation), cloud
controls, native controls/handover, cloud settings, and cloud/native lifecycle.
These tests use simulated device/API responses and do not establish physical
Modbus timing, which still needs reporter confirmation.
No physical charging or production deployment performed. Prepared for the
3.0.4b1 prerelease; stable 3.0.3 remains unchanged.

## 3.0.3 development history

### Modbus setup with an invalid initial power limit (#21 follow-up)

A successful first Modbus read could report a zero power-limit register. When
mode restoration was enabled and no power preference existed, optional seeding
raised `Invalid saved power limit` and aborted setup. Skip invalid initial reports
instead of storing them or inventing a replacement. Preserve raw telemetry and
existing saved intent; strict validation of stored preferences and user writes
is unchanged. The same optional initialization helper is used by cloud setup.

Regression evidence: six new invalid-report cases failed before the fix; 167
policy/configuration tests pass after it. The extended real-HA configuration smoke
loads actual Modbus entities from a zero-limit report, verifies no device writes,
and preserves a saved valid preference across reload with the same zero report.
The reporter's log does not include the register value, so hardware confirmation
is still needed. This fix is the 3.0.3b1 prerelease candidate for reporter validation; it is not
in a stable release. No production deployment accompanies this candidate.

### Household breaker range and Modbus troubleshooting (3.0.3b2)

Protocol v1.0.15 documents register 10026 as Household Circuit Breaker Rated
Current, 0–2000 A. Replace the Modbus entity's assumed 6–32 A slider with a
0–2000 A numeric box, preserving identity and reported values. Reject invalid
or fractional writes; never silently clamp to 32 A. This is a protocol range,
not a recommended installation setting. Cloud API limits are not inferred from
this Modbus register specification.

Add a bounded, thread-safe, cached trace of connection events, FC3 reads, FC6
write attempts/results (including the existing Start pre-reset), and allowlisted
numeric status/CP/communication/fault observations. The last 128 events and
since-load request counters are available in downloaded HA diagnostics; debug
logging records the same events with timestamps. No extra polling, raw register
payloads, account data, host or serial is added to this trace. Reload resets it.
Diagnostics are observational and cannot establish another client's activity.

The reporter confirmed beta1 setup succeeds and the reported power limit is
indeed 0.0 kW. Reload confirmation remains pending. Beta2 addresses the reported
63 A UI mismatch and gathers evidence for the separate charging interruptions;
it does not claim those interruptions are fixed. See [the test procedure](MODBUS_TROUBLESHOOTING.md).

## Remaining investigation

- Modbus connection-triggered charging interruptions reported in #16: removing
  unsolicited Stop writes addresses one software cause, not every hardware case.
- Original-HCA cloud Auto start: accepted API writes did not establish working
  hardware control. Keep capability gating; native TCP is the verified path.
- Native Auto start on other models/firmware: do not extrapolate the verified
  original-HCA configuration layout or local command envelope.
- Additional native mappings for extended settings require independent readback
  and safe hardware validation; missing data must remain unknown/unavailable.
- Natural token expiry and continuous MQTT telemetry coverage remain unverified.
  Preserve HTTP fallback and do not treat an MQTT connection as full coverage.

## Native fault research (not a runtime change)

Recovered the compressed original-HCA internal/history fault-label table and
verified its lookup offline. The current TCP status field uses a separate
summary domain: a traced reference path reports 2 for a nonzero condition bitmap.
Do not map that value to internal/history label 2 (`lack_money`). Numeric sensor
states and control logic remain unchanged. Details, test scope and remaining
current-bitmap research: [Native fault domains](NATIVE_FAULT_CODES.md).

Follow-up emulation traced command 108 end to end: 21 condition bits are exported,
12 have selector-backed reference labels, nine remain unnamed. The startup-timeout
bit is omitted, so empty details cannot mean fault-free. The marker does not
overwrite the detailed bits, which start later in the block. Offline decoder and
malformed/unknown-bit checks pass; one archived real zero-condition frame agrees.
Nonzero installed-firmware validation and passive freshness handling remain open.

### Passive native TCP fault diagnostics

Expose the latest validated command-108 report in downloaded diagnostics, with
monotonic age, reference-only labels, unknown bits and explicit incomplete coverage.
Require enrolled session identity plus matching serial/connector/layout. Clear
on disconnect/re-registration and suppress during cloud operation or switching.
Reject unknown optional layouts without disconnecting normal status/control.
No extra queries, acknowledgements, entities, notifications or control decisions.
The mapping has firmware-emulation evidence and one archived zero-condition
hardware report; nonzero installed-firmware validation remains outstanding.

Validation: 62 focused decoder/lifecycle/export tests pass. The runtime decoder
also accepted all 32 emulator-generated report payloads (with a valid test
enrollment envelope) and the unmodified archived real zero-condition frame.
Loopback tests verify no diagnostic traffic, session isolation, optional-layout
rejection, age handling, privacy and hiding details during cloud/switching.

All 107 existing native transport regression tests also pass; Ruff F checks and
whitespace validation pass. No production deployment or release was performed.

### #21 Modbus interruption follow-up: investigation bounded

Reporter confirms setup works; supplied trace excerpts show reads without writes,
EMS online/cloud offline, and recovery after polling is removed. A first-hand
report for the same model describes interruption on TCP connection alone.
No documented EMS takeover/timeout sequence or proven remedy was found in the
bundled protocol or inspected evcc implementation. Keep setup fixes separate
from unresolved interruption behavior; do not invent automatic register writes.
See the bounded optional connection-only comparison in
[Modbus troubleshooting](MODBUS_TROUBLESHOOTING.md). Exact reporter firmware and
new protocol evidence are prerequisites for further hardware-specific work.

A separate optional one-attempt HA-originated Modbus Start comparison is prepared
for #21: establish a known rollback, set/confirm a valid low power before Fast,
verify actual mode/power independently of saved intent, then one bounded Start
and verified Stop. Existing beta2 suffices; no speculative runtime change or
prerelease is required. Await reporter results before further implementation.

## Cloud household import-current range (issue #21)

- Fix the cloud import-current entity's incorrect 32 A ceiling. Use the official
  SEMS+ per-device `controlItemRanges` minimum/maximum; missing/null bounds use
  the official current client defaults of 0–2000 A. A reported 63 A now displays
  normally and remains editable within the device range, without artificial locks.
- This setting is the household incoming-current limit for dynamic load control,
  not the charger's output-current rating. Entity identities and existing names
  remain unchanged. Modbus and native TCP protocol writes are unchanged.
- Both cloud implementations share validation, preserve two-decimal requests,
  and reject invalid values without rounding or clamping. Recheck fresh metadata
  and device identity before explicit writes. An ACK never replaces telemetry.
- Invalid metadata or a report contradicting valid bounds makes the control
  unavailable; the original report remains in cached diagnostic data. No automatic
  corrective write is made. Missing metadata in a successful response differs from
  a failed metadata request, which does not authorize a write using broad defaults.
- Cache discovery per serial; do not add a metadata request to each telemetry poll.
  Refresh it before explicit current-limit writes. Native cloud settings also resolve
  missing metadata after local startup or reload with saved capabilities. Legacy
  entities retry discovery on explicit entity update or integration reload.
- Add shared regressions for both paths, API metadata propagation/caching and real
  HA platform tests. English, Czech, German and Spanish errors updated consistently.
  See `CLOUD_CURRENT_LIMIT.md` for evidence and remaining hardware validation limits.

## Prepare Fast-mode power from PV modes (#21)

Cloud-only and Modbus charge-power controls remain available in supported PV
modes. An explicit value there is saved as HA intent without switching mode,
writing a device register, or replacing reported telemetry. Selecting Fast then
applies and independently confirms that limit, including when automatic mode
restoration is disabled. A missing saved limit still requires an explicit user
choice; unknown/zero reports are not treated as a confirmed usable limit.
The saved power survives reload; the reported_power_limit attribute preserves
the device observation. Cloud session validation cannot establish Modbus firmware
compatibility or resolve the reporter's separate charging interruption.

Validation: 1350 full regression tests passed; the final 26 staged-power cases
also passed after strengthening timestamp simulation. Real HA entity smoke tests
cover both branches without external traffic. A real cloud-only development-HA
session on original GW11K-HCA confirmed PV selection, staging 4.2 kW without a mode
change, persistence across reload, verified Fast/4.2 kW, then measured charging
at 4.1 kW and Stop. The no-saved/zero-limit scenario is simulated; the physical
baseline already held 4.2 kW. The development capability fixture temporarily
exposed the known-working HCA Fast power control because discovery listed only
Dynamic_Load_Control. No Modbus hardware validation is implied.

Follow-up #21 comment 5816372927: reporter confirms 3.0.3b3 fixes cloud current
input and writes are reflected in cloud. Their separate timestamped-reader login
failure is addressed in development below, pending reporter validation. The vehicle sensor incorrectly showing not_plugged_in while
the owner confirms a connected cable was reproduced locally, including during
measured cloud charging; the development correction and physical cable evidence are documented below. No authentication error occurred
in the local staged-power session.

After the physical test, production ownership and both original charging
automations were verified restored: Fast, 4.2 kW limit, measured 0 kW. The
development entry is disabled again and its original options/capability data
were restored. No production code deployment or new release was performed.

## Cloud vehicle state and shared telemetry authentication (#21)

Source: https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/issues/21#issuecomment-5816372927

- Preserve the SEMS+ `vehConnStu` connection flag. An owner-assisted original-HCA
  FW1010 idle cable cycle confirmed 1 (connected), 0 (disconnected), then 1,
  while `workState` incorrectly remained `available_gun_no_insered` throughout.
  V3 independently changed Waiting_Stat01 -> Stat00 -> Stat01. Prefer the known
  flag over contradictory text; unrecognized explicit flags remain unknown.
  Absence keeps the legacy mapping. Connection alone never proves charging or
  completion; existing explicit completion and active-charging handling remains.
- Use one vehicle mapper for cloud-only and native cloud entities. Keep existing
  entity identities and translations; no new sensors or invented zero values.
- Remove the timestamped reader's independent `semsPlusAndroid` login. Reuse the
  integration's serialized SEMS+ web session, authentication fallback, request
  gate and login backoff. One rejected telemetry request may renew that shared
  session and retry once. If the renewed session is still rejected only by the
  telemetry endpoint, retain it for controls and pause telemetry attempts for
  30 seconds. Explicit rejected account credentials still propagate as auth errors.
- Keep V3 report identity/timestamps and fresh mode/power verification. Do not
  replace an independent device observation with an ACK or HTTP receipt time.
  Read-only physical probes verified V3 accepts this owner's existing web token.
  The reporter's Australian account still requires confirmation; local success
  does not prove its regional endpoint permissions.

The owner-assisted cable cycle did not start charging. Production integration
and both original charging automations were restored, with measured power 0 kW.
These changes remain in development, without a new release or production deployment.

Validation for the combined development changes: 1369 regression tests passed
in 211.09 seconds; focused cloud/authentication/vehicle tests passed (274).
Ruff F checks and git whitespace validation passed. A read-only smoke in the
restarted development HA exercised the actual shared-token reader and the detail
parser: both mapped the connected cable correctly, and the real vehicle entity
reported connected. Temporary probe registration was removed and development HA
restarted with its integration disabled. Production and both automations remain
restored. No charging command was issued for this cable/authentication validation.

## Interrupted sessions are not completed sessions

Remove suspended_ev/suspended_evse completion aliases inherited during the shared
vehicle mapper consolidation. Modbus uses these for start failure and PV/battery
shortage, not completed charging. An explicit connection flag remains authoritative;
without one, the vehicle state stays unknown. No new entity/state or translation
is needed. 154 targeted tests passed and the offline reference TCP mapper was
re-executed; TCP state 3 remains ambiguous. See [vehicle state evidence](VEHICLE_STATE_EVIDENCE.md).
Natural TCP session completion still requires a vehicle-confirmed target-reached
capture; no charging or production changes were made for this investigation.

Owner-assisted low-target-SOC test: cloud showed no measured charging over a
three-minute observation. Native Start remained state 1 at zero power until the
existing 45-second Fast guard sent Stop; states 3 then 0 followed. This is not
natural completion evidence. Keep the mapping conservative and investigate the
waiting/Start-timeout UX separately; see VEHICLE_STATE_EVIDENCE.md. One short
native Stop timed out, recovered through cloud; repeated TCP Stop was observed.

## Recheck delayed native Stop confirmation

The low-target-SOC investigation exposed a missing status-recheck path for ordinary
TCP Stop. It sent Stop once, queried status after 300 ms, then relied entirely on
unsolicited reports. A device returning an intermediate state and subsequently
waiting for another query caused confirmation timeout and deliberate peer closure,
which in turn invalidated telemetry. Protective Stop already polled repeatedly.

Ordinary Stop now repeats read-only status queries within the existing timeout.
It still transmits Stop exactly once; only the existing protective path may retry
Stop. No timeout extension, Start replay, relaxed Stop predicate or synthetic idle
state is introduced. Two loopback tests (starting and charging) fail before the
fix and pass after it. A nonterminal responder must still time out, close the
uncertain session, and never claim Stop confirmation.

The physical short attempt had fresh TCP reports until around Stop, followed by
missing fresh telemetry and timeout. The long attempt's protective Stop succeeded.
This supports the hypothesis but is not a packet-level proof of the original
incident. The invalid-frame log occurred later during recovery; do not treat it
as a proven cause. No further physical charging was performed for this change.

The UI freshness error also has a queueing component: the coordinator's status
request has a 10-second budget and shares the command lock with an ordinary Stop
(up to 30 seconds). A queued status timeout can mark the update unavailable;
it does not by itself prove that the receive loop or wallbox stopped working.
The decoded receive path runs independently of that command lock. Distinguish
last decoded report age, queued status-query failure and command confirmation
failure when reviewing the incident. No blanket suppression of update failures
or widening of freshness thresholds is part of this correction.

Follow-up correction: after a status-query timeout, accept an independently
received newer valid report only if the same TCP session remains available.
A failed query with no advancing report, a replaced/closing session, or an
explicit connection error still fails. The transport now treats a closing writer
as unavailable immediately, before asynchronous receive cleanup clears cached
telemetry. This prevents a closed socket from satisfying the recovery check.
A new regression failed before the coordinator fix and passes after it; negative
cases cover stale, replaced, unavailable and explicitly failed connections.
A real loopback test separately proves queued-query timeout does not block the
receive loop or cancel a pending Stop. No new telemetry is synthesized.

Validation: 110 transport regressions passed after the Stop polling change;
81 observation/handover/fallback regressions passed after the refresh correction.
The final immediate-closing fence passed five focused Stop/queue/closing tests
and eight existing disconnect/unload/cancellation tests. Ruff F and whitespace
checks passed. These changes are development-only; no additional charging,
production restart, deployment, push or release accompanied this investigation.

Physical follow-up on development HA: three ordinary Stops confirmed in 6.844,
6.047 and 3.953 seconds, each with one Stop transmission. No decoder/send errors
or failed cached updates occurred. During the third session the owner lowered
the car target and confirmed "charged" on the car; power and all currents fell
to zero before any Stop, while TCP state 2 persisted for over 100 seconds.
That code also occurs during charging/Start, so it must not be relabeled as
completed. The existing mapper regression now includes the exact connection
flag from this capture; all 43 observation/diagnostic tests pass. See
VEHICLE_STATE_EVIDENCE.md for evidence and limits. Production ownership and both
charging automations were restored, Fast/4.2 kW/0 kW. Temporary tracing and the
second run's development-only waiting override were removed, and development HA
restarted with its wallbox entry disabled. No production code was deployed.

Cloud comparison follow-up: after owner-confirmed target reduction during real
charging, four advancing V3 reports retained Charging at zero power; SEMS+
detail returned available with vehConnStu=1 and contradictory no-inserted text.
No explicit completed state was observed. Keep completion conservative; review
actual-energy-flow presentation separately from cloud session status. Cleanup
Stop required a subsequent regular status poll before the cached HA switch
confirmed Off. Production ownership was restored only after that confirmation.
See VEHICLE_STATE_EVIDENCE.md; no additional integration logic was changed.

Longer cloud validation corrects the short-window limitation: without any Stop,
V3 eventually reported Waiting_Stat02 and HA displayed finished_charging about
9.5 minutes after zero load. Approximately one minute later it returned to
Waiting_Stat01/connected. Fifteen minutes were observed; no resumed load or
watchdog intervention occurred. Keep the explicit completion mapper unchanged.
Cloud session state and actual energy flow remain distinct; the equivalent long
TCP test is still open. See VEHICLE_STATE_EVIDENCE.md for exact evidence.
Production ownership and both automations were restored, temporary capture removed.

## Actual energy-flow display without weakening session safety

The optional cloud/native charging-activity binary sensor now requires valid
measured power. Charging with zero load is Off; missing/non-finite/negative power
is unknown, never synthesized as zero. Native phase-current disagreement also
remains unknown. Failed updates, handover, old transport reports and stale device
timestamps cannot produce a trusted activity result. Cloud freshness uses the
existing ten-minute ceiling, evaluated on entity updates without extra polling.

Keep the reported wallbox state, command switch, entity IDs, translations and
conservative control helper unchanged. The same zero-load report must not confirm
Stop, permit idle-only changes or trigger ownership recovery. Verified native
state 2 / connection 1 / zero load now reports the vehicle connected, never
finished; explicit cloud completion remains transient and follows later reports.

Tests replay the physical cloud charging -> zero-load Charging -> completion ->
connected sequence, check controls stay independent, invalid/stale data, timestamp
boundaries/timezones, native current consistency and transport fencing. An isolated
real-HA smoke verifies on/off/unknown/unavailable serialization and handover.
No charging, production code deployment or release is part of this change.

Validation: all 1441 regression tests passed in 220.23 seconds. Independent
Astra high review approved the scoped runtime/tests with no actionable findings;
the reviewer separately passed 118 focused tests and the actual HA entity smoke.
Ruff F and whitespace checks passed. No new physical charging was needed: the
recorded scenarios were replayed in tests and actual HA serialized the results.

## Physical Start/Stop and live power regression validation

Four development-HA cycles passed: two TCP and two cloud, with live 5 -> 6 kW
and 6 -> 4.2 kW changes. Stable measured load increased/decreased in both routes;
requested limits were not assumed equal to consumption. Stop confirmed in
6.6/7.5 s over TCP and 11.7/23.8 s over cloud. No integration warnings/errors or
watchdog intervention. Production ownership and original settings were restored;
no production code deployment or release. See START_STOP_POWER_VALIDATION.md.

Open diagnostic issue: cloud reported_power_limit remained 4.2 kW while measured
load confirmed the 6 kW command. Investigate source semantics/freshness; never
substitute desired power for a device-reported value. Earlier narrow-step/ramp
assertions are excluded from successful physical validation, as documented.

Follow-up from saved responses: SEMS+ detail already reflected 5/6 kW while
V3-backed diagnostic limit remained 4.2. The 5 kW read was observed within
18.4 s; the 6 kW first captured read within 94.1 s (not a measured delay).
The sensor uses V3 set_charge_power, not SEMS+ chargePowerSetted. Investigate
source selection before adding retries or lengthening wait budgets. Longer V3
lag remains unmeasured; see START_STOP_POWER_VALIDATION.md for evidence limits.

## Correct cloud reported-limit source

The native-capable integration now reads its cloud diagnostic power limit from
verified SEMS+ configuration (chargePowerSetted), using the existing shared-session
CloudSettings cache. V3 set_charge_power was observed remaining at 4.2 kW while
SEMS+ correctly returned 5/6 kW and measured power responded. Native TCP still
uses its device readback. The sensor, power-control reported_power_limit attribute
and status set_charge_power attribute now agree on the same observed source.
No entity IDs, desired-power persistence, Start/Stop or measured-power semantics
change. This correction does not change the separate Modbus integration path.

Missing, invalid, failed or unverified configuration yields unknown/omitted
attributes, never HA's desired limit. Existing bounded configuration polling is
reused (five minutes between background reads); there is no extra per-telemetry
poll. Power/mode commands invalidate the cache even after uncertain writes, with
the existing subsequent coordinator refresh scheduling readback. In-flight reads
invalidated by a write cannot revalidate old data. External changes follow the
normal configuration cadence. SEMS+ readback is observed cloud configuration,
not proof of the effective hardware current ceiling.

Validation: 340 focused regressions passed, including source disagreement,
missing/invalid values, read failure, handover, TCP preservation, command failure
and in-flight invalidation. Ruff F and whitespace checks passed. A real idle-only
validation through development HA read 4.2 -> 6 -> 4.2 kW in both sensor and number
attribute; 6 kW was observed 19.57 seconds after the service request. Measured
power remained zero; no Start was sent. Production ownership and both automations
were restored and development entry disabled. No production code deployment,
push or release. Evidence: protocol_research/cloud_reported_limit_fix_20260924
on HP840. An initial harness readiness check incorrectly waited for a disabled
entity after restart; it failed before changing production ownership and was
corrected to check the configuration-entry API instead.
