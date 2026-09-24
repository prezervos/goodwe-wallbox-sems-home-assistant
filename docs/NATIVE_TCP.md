# Native Socket A TCP

## Scope and configuration

Socket A normally connects the wallbox to the GoodWe cloud. Local TCP temporarily
makes Home Assistant that endpoint. It is neither Modbus nor Bluetooth; Socket B
must be disabled. One entry owns one serial and one listener. A cloud-enabled
entry keeps its existing credentials and core unique IDs. The native branch
exposes only supported controls and observations, not the whole cloud feature set.

| Setting | Meaning |
| --- | --- |
| `native_host` | Wallbox IPv4 address for UDP management and direct peer filtering |
| `native_advertised_host` | HA host IPv4 address reachable from the wallbox |
| `native_port` | Published TCP listener port, default 18899 |
| `native_discover` | Setup-only read-only identity probe at the supplied address |
| `native_ingress_peer` | Optional trusted source-NAT proxy IP; empty means direct |

Publish the listener port through Docker if needed. Restrict the host firewall to
the wallbox or the explicitly trusted proxy. Source IP and serial checks are
identity checks, not cryptographic authentication. Do not expose the listener to
the internet. Serial/profile validation rejects unsupported models.

| Serial profile | Requested range (kW) | Hardware coverage |
| --- | --- | --- |
| `011KHCA` | 4.2 to 11.0 | GW11 HCA tested |
| `022KHCA` | 4.2 to 22.0 | Protocol tests only |
| `7000HCA`, `7000ACA` | 1.4 to 7.0 | Protocol tests only |

The profile is selected from serial characters `[1:8]`, following the reference
firmware. Requests must use 0.1 kW steps; invalid values are rejected, not rounded.
Actual load may be lower than the reported setpoint because of device/vehicle
behavior and current steps. A setpoint echo does not prove the effective limit.

## Intent, Start and power changes

`ChargeModePolicy` persists explicit HA mode and power requests in a versioned
HA store. Device telemetry never overwrites those preferences. Reload loads intent
without writing settings or starting charging. Native controls always use this
policy for power protection. The restore-mode checkbox independently controls
whether Start from HA restores the saved charging-mode preference. Without a
saved mode, or with restoration disabled, Start retains the mode reported by the
wallbox. Choose a preference using the charging-mode entity; there is no separate
initial-mode configuration field. Existing stored preferences remain valid.

A TCP Start verifies the selected mode policy, observes fault-free idle state, writes
the saved power request, and requires a matching fresh report in the same TCP
session. It then sends one Start and returns after delivery. This pre-Start power
write is a workaround for the observed loss of the effective power ceiling; it is not a claim
about every GoodWe firmware. No uncertain Start or setting write is replayed.

The user-facing API uses background supervision. The low-level transport also
retains a bounded synchronous Fast Start verification path for diagnostic tests:
primary reports must cover a ten-second window with no gap over six seconds.
That path is not used by HA service calls. Both paths are tested; do not confuse
synchronous diagnostic latency with the normal Start service latency.

Mode changes require Stop first. They restore the saved power in all three TCP
modes. A TCP power-number change preserves the mode. The legacy cloud number still
selects Fast; TCP semantics must not be assumed for cloud or Modbus.

Power-only requests supersede an older preparation but retain the explicit Start
intent. Stop, mode changes, handover and shutdown cancel pending Start. Duplicate
pending Start requests are rejected. Once Start may have reached the device,
failure triggers bounded Stop recovery rather than another Start.

## Observed state and protection

Supervision starts with a local Start or the first local power change in a session
that was already charging. Transport takeover alone does not alter power or arm
the guard. The connection switch exposes whether supervision is active.

- Fast starts in `starting`; if no charging is observed within 45 seconds, a
  protective Stop is requested.
- PV starts in `waiting`, without that no-load deadline. Its switch remains on
  so a zero-load request can still be stopped. Waiting is not proof of available
  solar power, accepted firmware regulation or session completion.
- Charging requires the decoded charging state, positive power and phase current.
  Stop confirmation requires idle/end state and zero power/current.
- Every mode uses the explicit HA power ceiling, with 0.5 kW measurement tolerance.
  A permitted increase updates the guard before transmission. Reductions allow
  ten seconds of settling; repeated requests do not extend older grace periods.
- Overload creates a persistent HA notification before Stop. Stop has at most
  three attempts, five seconds apart, within the bounded verification budget.
  Confirmed Stop and unconfirmed recovery are reported separately.
- While supervised, HA polls every two seconds and requests status when the last
  observation is at least one second old. Idle polling is five seconds. Unsolicited
  telemetry updates entities immediately.

This is software supervision of telemetry, not a hardware limiter. It does not
provide a watchdog independent of HA or calculate an energy-source budget. A PV
mode can still consume grid power without SEMS coordination, even with the user
ceiling correctly preserved. No local solar controller is implemented.

## Transport ownership and recovery

The Active transport sensor stays available during handover. It displays the
direction of the switch with a transfer icon, then waits for fresh cloud data when
returning to SEMS. Recovery-required and unavailable connections have warning
icons. Labels are translated; the existing entity ID is preserved. Measurements
remain unavailable until the new transport is verified. Charging, mode and power
controls accept bounded latest intent during handover, as described below.
This is a status indicator, not an animated progress bar or an estimated duration.

Before changing Socket A, `EndpointManager` saves the original endpoint and the
expected local endpoint. Restoration changes only a destination owned by this
entry; conflicting external settings are preserved and recovery remains pending.
The device's serial is checked through UDP management before configuration writes.

Handover is serialized with controls. Returning to cloud waits for a pending
protective Stop, restores the recorded endpoint, and closes the old TCP client.
A TCP link alone is not proof of cloud recovery. Cloud entities remain unavailable
until a device report newer than the durable handover boundary arrives. Naive SEMS
timestamps use HA's configured timezone; future timestamps are rejected.

On reload or startup, a pending journal is restored before normal operation. No
Start is replayed. Explicit manual TCP preference is persisted separately and
reapplied immediately after opening the listener, without waiting for cloud reads.
Shutdown still restores the owned endpoint before closing the listener, so reload
can include a brief physical reconnect; the saved user preference remains TCP.
Selecting cloud manually clears the TCP preference. Automatic fallback never
changes that manual preference.

If TCP was selected automatically before reload, the saved outage hint starts a
bounded 60-second cloud verification trial instead of the normal 90-second failure
debounce. It requires a fresh report after restoration; slow setup refresh waits
are limited to ten seconds. If verification fails, TCP takeover begins, adding
its normal connection time. Authentication rejection still requests reauthentication
and does not authorize takeover. A pending journal also supplies this hint after
a crash between takeover and hint persistence. A successful verified cloud read
clears it. Disabling automatic fallback disables this expedited startup recovery. A failed restoration
retains its journal. An unavailable HA host cannot execute this recovery until it
runs again. Automatic fallback is opt-in as described below; an independent
host watchdog is not included.

## Module map

| Module | Responsibility |
| --- | --- |
| `native_protocol` | Bounded frame decoder, identity checks, decoded telemetry, allowlisted encoders |
| `native_energy` | Single-block cumulative storage read and independent integrity checks |
| `native_energy_polling` | Opt-in idle polling and lifetime-source availability |
| `native_transport` | Single peer, session fencing, serialized commands, verification and recovery |
| `native_session_guard` | Independent actual-power supervision after Start returns |
| `native_start_verification` | Primary-report window for synchronous diagnostic Start |
| `native_adapter` | Mode-policy adapter and saved-power restoration |
| `native_endpoint` | UDP management, durable ownership and restoration |
| `native_discovery` | Optional read-only identity discovery |
| `native_coordinator` | HA lifecycle, polling, transport routing and cloud freshness |
| `native_fallback` | Opt-in debounce, bounded cloud trials and recovery backoff |
| `native_intent` | Latest pending user choices, expiry, ordering and cancellation |
| `native_preflight` | Read-only endpoint/API reachability before discretionary cloud trials |
| `native_connection_intent` | Durable manual TCP preference and prior-outage recovery hint |
| `native_entities` | Stable core identities, requested vs observed state, controls |
| `native_power_limits` | Serial-derived model ranges and exact step validation |
| `charge_mode_policy` | Durable user intent, request serialization and cancellation |
| `charge_mode_adapter` | Existing cloud/Modbus observation and control adapters |
| `cloud_observation` | Timestamped SEMS v3 observation, separate from control endpoints |

Wire format: `AA F5`, little-endian length, information byte16, sequence, little-endian
command, 22-byte identity, body and additive checksum. Commands1/5/7 cover parameter
reads/writes, Stop and Start. Parameters47/48 are power/mode. Status104/2104 provides
measurements. Registration/heartbeat/status acknowledgements do not acknowledge or
retire stored charging bills. Unknown operations, firmware writes and history
retirement are deliberately not encoded. Firmware reverse-engineering and raw
physical traces belong in the external research archive, not the runtime package.

## Optional diagnostics and cloud fields

Phase current and voltage entities are diagnostic and disabled by default. Their
telemetry is still decoded for internal charging verification. Existing user entity
settings are preserved. In cloud mode, unsupported phase/fault values are unavailable.
A cloud-reported zero session time is displayed as zero; nonzero SEMS time units
remain unverified and are not guessed. TCP session duration uses decoded seconds.

## Observed activity and vehicle state

The existing Charging switch retains its control behavior for compatibility. A PV
request can remain enabled while waiting without drawing power. The read-only
Charging in progress binary sensor separately reports observed charging, confirmed idle,
or unknown when telemetry is missing/inconsistent. It does not use startStatus as
proof of actual charging. Automations needing actual consumption can use this
sensor or measured power without changing existing Start/Stop service targets.
The binary sensor is disabled by default for new entities; existing registry
choices are preserved. The multi-state sensor is named Wallbox status.

Vehicle state reuses the legacy workstate unique ID and translation. Recognized
cloud states are mapped explicitly. In TCP, measured charging proves a connected
vehicle. For confirmed idle state 0 with zero power/current, connection byte 0/1
maps to unplugged/connected, based on two physical cable cycles on original HCA
FW1010. Other codes or inconsistent observations remain unknown. Zero power alone
does not prove an unplugged vehicle, and no stale cloud state is carried into TCP.

The optional **Wallbox total energy** sensor exposes the native lifetime counter
in kWh, separately from the legacy cloud session-energy entity. It uses energy
device class and `total_increasing` statistics, passing through verified raw
counter values without artificial offsets or a fabricated `last_reset`. HA handles
its normal initial baseline and counter resets. Actual factory-reset behavior has
not been physically tested; simulated valid zero/reset values are covered.

The sensor is diagnostic and disabled by default. Enabling it starts idle-only
polling at most every five minutes, plus a read after an observed active session
returns to idle. Controls already queued/in flight take priority. A read itself
holds the transport lock for at most five seconds. It never requests bulk history
or sends history-retirement ACKs. Disabled entities produce no storage queries.

Cloud, handover, lost sessions and invalid/missing snapshots make this sensor
unavailable; they never create zero readings or use cloud session energy. Within
the same healthy TCP session, the last successful value remains while charging;
no continuous live-energy update is implied. Deltas are recorded when the next
valid snapshot arrives, possibly after a cloud interval. This is suitable for a
cumulative total, not precise allocation to charging minutes or tariff periods.

The low-level reader checks the serial, one-block type/length/count, magic and
embedded storage checksum. Failed/cancelled sent requests disable additional
storage reads until reconnect because response sequences are independent. Status,
controls and protection remain operational. Two physical integration reads agreed
at 9181.15 kWh; earlier archived frames also passed. Never replace the legacy
`-energy` unique ID or splice that session counter into this lifetime source.

## Connection maintenance and diagnostics

Reconfigure validates access to the existing serial before saving host/port or cloud
credentials. Reauthentication uses the same identity check. Password fields are not
prefilled; an empty password keeps the existing credential. Changing connection
settings is blocked while TCP owns the endpoint, a handover is running, or recovery
is pending. Return to cloud before maintenance. Existing entity unique IDs, options
and saved mode/power intent are retained. Validation uses reads only; successful
saving reloads the integration and does not replay Start.

The Download diagnostics action exports an allowlist of numeric observations,
requested settings, connection/freshness and protection flags. It excludes account
credentials, serials, network addresses, raw packets and free-text backend errors.
No device queries or writes are made to generate the snapshot.

## Automatic cloud fallback

Enable `native_auto_fallback` in the native connection options of an entry with a
cloud account. It defaults to off and does not change existing installations.
Manual TCP selection pauses automatic actions and preserves this preference across
reload/restart. A manual cloud selection clears it and pauses automatic actions
until reload. The connection status attributes and downloaded diagnostics expose
the current pause.

Three consecutive failed cloud observations spanning at least 90 seconds qualify
for local takeover. Successful observations reset the window. The normal polling
interval still applies, so 90 seconds is a minimum, not a promised deadline.
Authentication rejection blocks takeover until a successful observation; repair
credentials instead of treating account failures as a device outage.

When enabled, cloud observations also require a valid timestamp no older than ten
minutes. This conservative initial ceiling avoids mistaking unchanged HTTP results
for live telemetry. It is a freshness ceiling plus the failure debounce, not a
claim that SEMS reports every ten minutes. Future/missing timestamps cannot prove
health. Calibrate this ceiling against physical reporting cadence before rollout.

All automatic writes use the existing policy lock and durable endpoint ownership
checks. Failed transitions back off five minutes. Takeover never sends Start or
changes mode/power; the usual verified preparation applies to the next explicit
Start. Manual requests and shutdown supersede queued automatic transitions.

After successful fallback, the first cloud-return trial is due after 30 minutes.
Trials require confirmed idle local measurements and no pending Start/PV wait.
The check repeats after acquiring the control lock. Socket A is exclusive: API
reachability while local cannot prove the device-to-cloud path is healthy.
Before a discretionary trial, a read-only preflight checks TCP reachability of
the original host/port from the ownership journal and an authenticated API read
for the same serial. Local polling and controls stay available; the probe holds
no control lock and never publishes its cloud data. Old/offline cloud telemetry
is expected while local and does not itself fail the preflight. No TLS client
certificate is fetched, no device login is simulated, and no telemetry or control
frames are sent to the device server. A reachable port is not proof of TLS or
application health. The probe has a 20-second total budget and a five-second
TCP connect/close budget. A failed probe keeps TCP and retries after five minutes.
Rejected credentials request reauthentication instead of switching transports.
Manual overrides, shutdown and intervening transport changes discard probe results;
charging eligibility is rechecked under the existing control lock. Emergency
recovery of an already-failed local transport bypasses this discretionary filter.

A successful preflight only permits a real trial. A trial restores the cloud
endpoint and allows 60 seconds for a fresh, matching
device report newer than handover. A failed trial returns to TCP and backs off
60 minutes, then at most 120 minutes. A verified return resets the backoff.
Unknown/active local charging postpones discretionary return trials. Three failed
local status polls spanning at least 15 seconds instead qualify for recovery to
cloud, even during the dwell period; failed recovery attempts back off five minutes.
This recovery preserves charging intent and waits for any pending protective Stop.

The existing translated Active connection sensor shows direction and verification
progress. Its diagnostic attributes include reason, consecutive failures, pending
trial and retry delay. Missing measurements remain unavailable, never synthetic
zero. No command replay or independent-host watchdog is implied.

Production gate: validate stale/offline data, short/sustained outage, failed return,
recovery ownership, manual override, concurrent controls and lifecycle cancellation
with unit and real HA loopback tests, then bounded physical handover checks. Do not
claim production readiness solely from simulated results.

## Implemented latest-intent buffering and measured return budget

Three successful idle physical return measurements on 2026-09-17 observed fresh
cloud reports after 38.19, 37.41 and 39.02 seconds from the return request. HA
confirmed cloud readiness after 38.19, 42.47 and 39.03 seconds. Endpoint restoration
service calls took 6.36-6.67 seconds. TCP takeover took 11.00-30.53 seconds. An
additional measurement attempt failed and was recovered by the independent guardian;
its child error output was not captured, so do not treat these as three consecutive
flawless transitions or a guaranteed upper latency bound.

The automatic cloud trial budget is now 60 seconds, with active polling rather
than waiting for the normal idle poll interval. Individual refresh waits are bounded
by the remaining budget and ten seconds. TCP restoration still adds reconnection
time. The shorter 30-second budget is not supported by these observations.

Charging, mode and power controls remain usable during transport handover/cloud
freshness verification. Requests received then are accepted as pending intent, not
as confirmed physical changes. Only one latest value per logical control is held;
Start and Stop share a slot. Pending choices are visible in Active connection
attributes and diagnostics. Measured sensors stay unavailable until verified.

A batch expires after 120 seconds; repeated clicks do not extend it. Stop precedes
pending settings. Settings precede Start; cloud power is applied before mode because
the legacy cloud power operation selects Fast, while TCP applies mode before power.
Normal validation and active-charging mode restrictions remain in force. In-flight
preparation is fenced when new choices arrive. Failed or uncertain Start is never
replayed; a newer Stop can still be executed. Errors/expiry remain in logs and pending-intent diagnostics without persistent
notifications. Routine cloud-control preflight and TCP-ready transitions are logged,
not posted as persistent notifications; protection-triggered Stop alerts remain separate.
A duplicate Start is a no-op only after matching charging is confirmed; cloud reports
must advance before that confirmation. An explicit Start after a newer Stop is a new
intent. Known pre-delivery supersession preserves the latest settings, while an
uncertain transmitted Start is never replayed. Restart/unload discards pending requests
without replay.

Unit tests cover bursts, supersession, prerequisite ordering, expiry, shutdown,
failed prerequisites and uncertain Start. Real HA loopback tests exercise deferred
service calls through an actual endpoint transition with no obsolete Start sent.

## Portability and certificates

Native mode opens a plain TCP listener in HA and temporarily points the wallbox's
Socket A at that listener. Cloud TLS credentials remain in the device module;
the integration neither downloads nor installs a device certificate or private
key. Returning to cloud restores the original endpoint including its TLS flag.

This is portable configuration, not universal device compatibility. The enrolled
model must speak the supported HCA/ACA native protocol, expose the supported UDP
management interface, have Socket B disabled and be able to reach HA's advertised
LAN address/listener port. Validate network routing/firewall/container port mapping.
Serial and peer checks are not encryption; restrict this plain connection to a
trusted LAN. GW11 HCA has physical coverage; other accepted profiles still require
hardware verification. No Bluetooth proxy or HP840-specific runtime is required.

Without saved power intent, native control seeds the supported model minimum (4.2 kW for GW11 HCA, 1.4 kW for supported single-phase GW7 profiles). Existing stored intent always wins. The charging-power entity is the only user-facing power-limit setting; setup and options no longer expose a separate initial power.

Enabling mode restoration in options explicitly adopts the currently reported mode, replacing any stale stored preference. This occurs only on final form submission, not when opening or abandoning a page, and does not write wallbox settings. Unknown/unavailable mode blocks enabling with a translated error. Subsequent reloads and unrelated options changes preserve the preference; external mode changes do not overwrite it while restoration remains enabled.


## Verified session energy

The existing session-energy entity decodes HCA status 104/2104 in kWh. No new
polling or helper sensor is needed. The device resets its native value after Stop;
missing data is not converted to zero. Cloud/TCP observations remain separate.
Physical handover, Stop, return and Recorder sums passed on the original GW11 HCA.
See [validation and limitations](VALIDATION.md) for the tested scope.


### Failed controls and optimistic presentation

Deferred failures remain visible in diagnostics and logs. They do not create
persistent notifications for routine control or transport errors. Protective
alerts for failed safety stops remain separate. A newer explicit Stop survives
failure of an earlier setting or Start; uncertain writes are never replayed.
Failed/cancelled settings clear their pending presentation and schedule readback,
without overwriting newer requests or reports. A pending mode is presented by the
select entity only; dependent entities continue to use reported device data.
