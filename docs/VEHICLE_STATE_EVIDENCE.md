# Vehicle connection versus session completion

## Verified sources

- Original HCA FW1010 owner-assisted idle cable cycle, 2026-09-24: SEMS+
  `vehConnStu` returned 1 -> 0 -> 1; V3 returned Waiting_Stat01 -> Stat00 -> Stat01.
  SEMS+ `workState` remained incorrectly `available_gun_no_insered` throughout.
- The bundled GoodWe Modbus protocol, register 10017, distinguishes 4 (completed),
  8 (start failed), and 10 (interrupted because PV/battery energy is insufficient).
  The adapter aliases 8 to `suspended_evse` and 10 to `suspended_ev`. Neither alias
  proves completion or even the current cable state.
- The Open Charge Alliance OCPP 1.6 compliance test TC_005, page 15, also reports
  SuspendedEV/EVSE before transaction termination, even after EV-side disconnect.
  This corroborates the distinction but does not establish proprietary GoodWe
  field semantics or justify assuming connected from a suspended value.
  Source: https://openchargealliance.org/wp-content/uploads/2025/02/CompliancyTestTool-TestCaseDocument.pdf
- Re-executed the archived reference-firmware TCP state mapper on HP840, offline:
  internal states 1, 5, 6 and 7 all produce TCP state 3 under the baseline Fast,
  no-fault/no-lock conditions. The mapping is many-to-one. This is reference-image
  evidence, not a complete installed-firmware state-machine validation.
- The archived 2026-09-21 cloud session returned last-session code 8 after manual
  Stop while fresh V3 reported Waiting_Stat01 (connected). Thus code 8 does not
  establish battery-full completion. Its test summary did not capture charging
  synchronously, so it must not be presented as validation of natural completion.

## Runtime policy

Remove suspended-to-finished aliases from the shared vehicle mapper. Known
`vehConnStu` still gives connected/disconnected; absent connection evidence gives
unknown. Existing explicit completion mappings remain for compatibility, but
`finished_charging` does not claim battery SOC 100%. Do not introduce a universal
`charged` alias or infer completion from zero power, elapsed time, Stop ACK,
last-session code 8, or native TCP state 3/4.

The previous shared-mapper consolidation copied the old sensor's suspended
aliases and contradicted existing release notes. This development correction
removes those aliases for both cloud-only and native cloud entities. Entity IDs,
translation keys and control logic are unchanged; no new enum is introduced.

## Validation and open boundary

154 targeted sensor, observation, native-entity, UI and Modbus-diagnostic tests
passed. Regressions cover suspended reports with/without explicit connection,
and uncertain TCP states including 3 and 4. Existing translations remain valid.
No hardware commands, charging, production changes or release were performed.

To validate genuine native completion, capture a session that ends because the
vehicle reaches its target, with a confirmed vehicle-side reason, and compare
status/history frames against manual Stop and temporary suspension. A brief
Start/Stop test alone cannot establish that distinction. Until then, keep native
completion unknown instead of adding an unverified state mapping.

## Owner-set target below current SOC, 2026-09-24

The owner set a vehicle target below current SOC and authorized Start plus time
for the vehicle to respond. Tests ran through development HA, with production
ownership and charging automations temporarily paused. Fast limit stayed 4.2 kW.

- Cloud comparison: 45 cached samples across about 177 seconds after Start
  returned, with no measured power. Vehicle entity transitioned connected ->
  unknown -> connected; wallbox remained standby. This run did not capture the
  raw V3 workstate behind the transient unknown, so do not assign it a meaning.
- Extended native comparison: connection byte remained 1, current/power stayed
  zero, state moved 0 -> 1. The existing Fast Start guard timed out (45 seconds
  without observed charging) and issued protective Stop. Only afterwards did
  native state 3 appear, then 0. This does not establish natural target completion.
- An earlier short native attempt had an unconfirmed Stop / telemetry timeout;
  cloud recovery independently showed zero power and charging off. The extended
  comparison subsequently confirmed protective Stop with fresh TCP reports.
  A later invalid-frame log occurred during recovery; causality is not established.

These runs are consistent with the owner-configured target preventing energy
uptake, but no vehicle target/SOC reason was reported in the captured native
fields. A target already exceeded before Start is also different from reaching
that target during an active session. Do not relabel native state 1 or 3 as
finished_charging from this experiment.

Follow-up: capture natural vehicle target completion, or a separately identified
development test with an explicitly extended Start-observation deadline while
retaining power-limit protection. The current 45-second guard is a confounder
for longer native waiting experiments. Do not silently change production safety
behavior or treat this case as a proven communication failure.

## Live target reduction and Stop recheck, 2026-09-24

Three further development-HA sessions reached measured charging (up to 4.1 kW,
Fast intent 4.2 kW). Ordinary Stop confirmed in 6.844 s and 6.047 s in the first
two attempts. Each sent Stop once; repeated status reads observed the eventual
zero power and phase currents. No decoder/send failures occurred. All 52 cached
updates succeeded; maximum report age was 2.537 s. This validates the fix on
this device, not the root cause of the earlier uncaptured timeout.

In the third attempt the owner lowered the vehicle target while charging and
confirmed the vehicle displayed "charged" (Czech: "nabito"). No integration Stop
preceded the loss of load. Status 2 / connection 2 / 4.1 kW changed through
connection 1 with decaying current to status 2 / connection 1 / zero power and
all phase currents zero. That tuple persisted for over 100 seconds before the
explicit cleanup Stop. State 3 appeared only after that Stop, which confirmed
in 3.953 s. TCP reports remained fresh throughout.

This is vehicle-confirmed target reduction during charging, not natural attainment
of an unchanged target. It proves TCP state 2 does not unconditionally mean active
energy transfer. It does not establish a unique completed-session discriminator:
state 2 at zero power also occurs during Start. Keep completion unknown; do not
map 2, 3, or zero power to finished_charging. The current conservative mapper
already handles the captured tuple without inventing completion.

Evidence on HP840: protocol_research/stop_fix_physical_20260924 and
protocol_research/target_soc_live_reduction_20260924 (timestamps, cached HA data,
metadata-only TCP traces and owner confirmation). Temporary tracing did not add
hardware queries. The second attempt alone extended its development-only Start
wait to 210 s with power protection unchanged; charging began within the normal
45 s deadline, so the extension did not influence that result. The third attempt
used the normal guard. No additional charging should be inferred from unit tests.

## Cloud target reduction during charging, 2026-09-24

The owner raised the AC target before Start, then lowered it below current SOC
only after cloud telemetry confirmed 4.1 kW (later 4.2 kW) at the saved 4.2 kW
Fast limit. The owner clarified that the vehicle reported charging completed
according to plan, its usual target-SOC completion message. This is target
reduction during charging, not natural attainment of an unchanged target.

Passive capture retained allowlisted values from ordinary shared-session V3 and
SEMS+ requests, with HTTP receipt times distinct from device lastUpdate. No
polling interval was changed. One additional SEMS+ detail comparison read used
the same authenticated session and request gate after vehicle-side completion.

Before any cleanup Stop, four V3 reports retained status
EVDetail_Status_Title_Charging, empty workstate and power 0. Device timestamps
advanced from 18:09:21 through 18:10:20; first and last HTTP receipts were about
72 seconds apart. Thus the persistent Charging label was not simply one reused
stale response. No Waiting_Stat02 or other explicit completion code was captured.
The SEMS+ comparison returned available / available_gun_no_insered, vehConnStu=1,
startStatus=false. Its chargePower=4.2 is a configured power field here and must
not be presented as measured load. No unambiguous completion discriminator exists
in these captured fields; the contradictory no-inserted label must not override
the verified cable flag.

HA reflected V3: status charging, vehicle connected, Start switch on, measured
power 0. Follow-up: distinguish the cloud-reported session state from actual
energy flow without inventing target completion or changing automation semantics
blindly. A zero-power Charging report also appeared during Start in this run.

Cleanup Stop was requested at epoch 1790266258.040 and returned at 1790266264.575.
The immediate cached switch was still on, so the harness conservatively retained
development ownership instead of declaring restoration. A later normal V3 poll
at 1790266294.035 reported fresh Waiting/Waiting_Stat01, zero power. Off/zero was
then checked before restoring production; no TCP recovery was used. Do not infer
vehicle completion from Waiting after this explicit cleanup Stop. The independent
watchdog log contained no intervention.

Production entry and both original charging automations were verified enabled,
Fast 4.2 kW, measured 0 kW and EMHASS demand 0. Temporary capture was removed;
development HA restarted with its wallbox entry disabled. No production code
change, push or release. Evidence: protocol_research/cloud_target_reduction_20260924
on HP840 (cloud_trace.json, cached.jsonl, diagnostics.jsonl, events.jsonl,
owner_confirmation.json, detail_after_vehicle_stop.json, restoration_verified.json).

## Fifteen-minute cloud wait: delayed completion confirmed, 2026-09-24

This longer experiment supersedes any inference from the short windows that the
cloud cannot report completion. It does not change the factual short-run traces.

The owner raised the AC target, cloud Start reached measured 2.8 then 4.1 kW at
Fast 4.2 kW, and the owner lowered the target below current SOC. The vehicle again
reported charging completed according to plan. From the first cached zero-load
observation, development HA observed 902.246 seconds before its cleanup Stop.
No observation watchdog intervention or resumed energy flow occurred. Production
entry and original charging automations were paused for exclusive dev ownership.

Normal V3 responses show the following device-timestamp sequence:

| Device timestamp | Status | Workstate | Power |
| --- | --- | --- | --- |
| 18:17:37 | Charging | empty | 0 kW |
| 18:27:07 | Waiting | Waiting_Stat02 | 0 kW |
| 18:28:04 | Waiting | Waiting_Stat01 | 0 kW |

The completion report arrived 569.842 seconds after the first zero-power response
(9 min 29.842 s). HA displayed finished_charging 571.399 seconds after its first
cached zero-load observation; the existing translation/mapping already worked.
The next V3 transition to connected arrived 59.347 seconds after the completion
report. These are observation/receipt intervals, not a measured firmware timer.
No user/test Stop preceded either transition. The wire origin of the delay
(wallbox versus cloud processing) is not established by this experiment.

The 15-minute window contained 30 V3 responses with 28 distinct device timestamps.
Regular polling intervals and shared authentication were unchanged; capture did
not issue additional cloud requests. MQTT diagnostic counters also advanced,
but these alone do not prove the payload or trigger responsible for completion.

Consequences: retain explicit cloud completion mapping. Do not infer completion
from zero power and do not latch the transient completed state indefinitely.
Actual energy flow may warrant separate treatment from the cloud session state:
Charging can persist at zero power for minutes, and the same combination occurs
during Start. Review existing entity/automation semantics and missing/stale-data
handling before changing charging_active or the command switch. No runtime
mapping change was made for this experiment. A comparable long native-TCP wait
has not yet been performed; do not extrapolate this cloud code to native state 2.

After all 15 minutes a bounded cleanup Stop was issued; Off/zero was confirmed.
Production entry and both original automations were restored; Fast 4.2 kW,
measured 0 kW, EMHASS demand 0. Temporary capture registration/code was restored
and development HA restarted with its wallbox entry disabled. No production code
deployment, release or push. Evidence: protocol_research/cloud_completion_long_wait_20260924
on HP840, including cloud_trace.json, summary.json and owner_confirmation.json.
