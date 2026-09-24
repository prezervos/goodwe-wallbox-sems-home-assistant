# Modbus setup and charging interruption checks

## Household current limit

Register 10026 is **Household Circuit Breaker Rated Current**, range **0–2000 A**
in the bundled [GoodWe Gen2 protocol v1.0.15](modbus-protocol.md). This is not the
EV charging current. A reported 63 A is inside that register range. The numeric
box preserves the entity identity and unit; the writer rejects invalid values
rather than silently clamping them. The protocol range is not a recommendation
to raise a site's configured limit. Leave the existing installation value alone
for the following diagnostic test. Cloud API bounds are a separate contract.

## First: passive verification

1. Install 3.0.3b2 and restart HA. Keep charging-mode restoration enabled to
   exercise the original setup failure. Confirm setup and one reload succeed.
2. Check that the Modbus import-current entity shows the reported 63 A in a
   numeric box with range 0–2000 A. Do not change it just to test the range.
3. Enable debug logging from the integration's menu. Observe two normal polls
   without issuing controls, and note the approximate time.
4. Download integration diagnostics while the Modbus entry is still loaded,
   then disable debug logging to obtain the debug log. Do this before unloading,
   reloading or changing transport, which resets the in-memory trace.

Diagnostics contain since-load connection/read/write-attempt counts and the last
128 protocol events. This export itself makes no device request. It excludes raw
register blocks, host, serial and account data. Existing debug logs may contain
identifiers; redact passwords, tokens, account/device details before sharing.

## Optional: distinguish polling from controls

Only do this when a short normal charging session is convenient. Do not change
DLM, EMS dispatch or the household breaker setting for this investigation.
Temporarily prevent HA automations from issuing wallbox controls, noting their
previous state so they can be restored afterwards.

- With Modbus polling active, start charging using the official GoodWe app.
  Do not press Start, Stop, change mode or change power in HA. Record whether
  charging aborts, the elapsed time and the approximate timestamp.
- Download diagnostics immediately after an interruption, before a reload.
  If no interruption occurs, stop after a short observation (about two minutes)
  using the official app and record the outcome; a longer test is not required.
- Restore any temporarily disabled automations. Use cloud mode afterwards if
  Modbus continues to interrupt charging.

An interruption with **zero FC6 write attempts from this integration** rules out
its control writes during that observation window. It does not prove a firmware
fault: connection contention, reads, another controller or inverter coordination
remain possible. If writes occurred, the trace shows register/value and outcome;
only then choose a separate, specific command test. Avoid repeated Start clicks.

The existing Modbus Start sequence writes register 10060 = 1 (pre-reset/Stop),
then 10060 = 2 (Start). Both are now visible; this prerelease does not change the
sequence, polling cadence or per-operation connection lifecycle. An acknowledged
write is not proof of the final physical state; an unacknowledged attempt may
still have reached the wallbox. Diagnostic collection never retries a control.

Please include model, firmware, exact integration version, timestamps, the action
used to start charging, whether other HA controls/automations were active, and
both the diagnostics JSON and redacted debug log when reporting results.

## Bounded EMS investigation: issue #21, 2026-09-24

[Reporter follow-up](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/issues/21#issuecomment-5814983173)
confirms successful Modbus setup. Its supplied log excerpts contain 25 FC3 read
requests and no traced writes. Communication status 37 decodes to Wi-Fi,
inverter and EMS online, with the IoT-cloud bit clear. The reporter observes
charging resume and cloud return about three minutes after disabling Modbus.
This is an observation, not a documented 180-second timeout contract. The
excerpts do not establish all activity outside their recorded windows.

Register 10000 (EMS Energy Dispatch) is a minimum-power command, not a documented
EMS ownership-enable switch. Register 10157 selects gateway/transparent versus
normal IoT communication; it is not documented as a fix for read-induced charging
interruptions. The bundled v1.0.15 register specification does not establish an
EMS takeover/release sequence, heartbeat requirement or three-minute lease.
The reported zero power limit is suspicious but does not establish causality.
Do not automatically set power, write Start, change transparent mode or block
cloud access based on this evidence.

A [first-hand evcc report for the same GW7K-HCA-20 model](https://github.com/evcc-io/evcc/discussions/30669)
reports interruption even on TCP connection establishment and a zero charge
command register. Its experiment with transparent mode did not resolve the
problem. This is corroborating user evidence, not a manufacturer guarantee or
proof that every firmware behaves identically. The discussion also references
our #16, so do not count all linked reports as independent confirmations.

The [evcc driver](https://github.com/evcc-io/evcc/blob/master/charger/goodwe.go)
uses the same published register meanings and explicitly derives from the HA
Gen2 integration. It forces Fast mode and writes ordinary power/start settings;
no separate documented EMS ownership handshake was found in that driver.
The template's two-session claim is not vendor-verified and must not be promoted
to a confirmed hardware limit for this installation.

### One optional discriminating check, not a repair

First request exact charger and Wi-Fi/Bluetooth firmware versions, plus whether
other local Modbus clients are present. Existing diagnostics may already contain
the required versions. Do not request credentials or unredacted account data.

If the reporter has not already tested a TCP connection with **zero application
bytes**, a single optional comparison can separate connection establishment from
register reads: disable the HA Modbus entry and any other local Modbus polling,
record previous settings, and wait until normal cloud operation returns. During
a convenient ordinary app-started charging session, open one TCP connection to
the configured charger port, send nothing, and close it after at most five
seconds (no retries). Observe through SolarGo/car, without Modbus readback, and
record timestamps and cloud/EMS state before and after. Restore the original
client/automation settings after the observation; if charging remains suspended,
use the previously working cloud/app path. Do not change router, inverter,
transparent mode, current limits, DLM or EMS dispatch for this check.

If connection alone interrupts charging, changing read blocks cannot prevent
that trigger. If it does not, the existing read-related result remains, without
proving a particular register is responsible. Neither outcome alone authorizes
a persistent-connection workaround or automatic control writes.

Stop this investigation after that comparison unless new firmware-specific
protocol evidence arrives. No stable-release claim of resolved interruptions,
no speculative prerelease fix, and no requirement to block unrelated fixes.

## Optional controlled Modbus Start comparison (#21)

This is a user-assisted hardware test, not an automatic workaround. Use the
existing 3.0.3b2 controls, only when a brief charging session is convenient.
Record original SolarGo mode/positive power limit and HA saved preferences first.
A zero Modbus limit cannot be restored through the valid power-control range;
if no known valid original limit exists, do not change settings for this test.
Keep installation settings, DLM, EMS dispatch, Auto start and transparent mode
unchanged. Temporarily prevent competing clients/automations and record their
original enabled states. Confirm charging is stopped before preparation.

1. Set the lowest valid power offered by HA for the actual model before selecting
   Fast. For the reported 7 kW single-phase model this is typically 1.4–1.5 kW;
   use entity bounds, not a forced 1.4 kW value.
2. After a completed read, check downloaded diagnostic measurement
   `set_charge_power`, not the number entity or `requested.power_kw` (both can
   retain explicit intent). A failed, zero or mismatched readback ends the test.
3. Select Fast, then independently verify diagnostic `chargeMode=0` and the same
   `set_charge_power`. The Modbus mode adapter reapplies the intended power after
   Fast and the enabled policy verifies it. Do not disable verification to get
   past an error. Already-matching mode/limit may require no additional write.
4. Press HA Start once. Existing wire behavior is identity read, FC6 register
   10060=1 (pre-reset), then FC6 10060=2 in the same connection. These are expected
   trace entries, not unexplained background controls. Lost acknowledgements do
   not permit replay. Observe physical current/power and the car for at most
   60 seconds; stop earlier for unexpected power. Status text alone is not proof.
5. If charging started or delivery is uncertain, send Stop and verify the car
   stopped plus zero measured power/current. Use official-app/vehicle Stop if
   local Stop fails. Save diagnostic JSON and debug log before unloading/reload.
6. Restore the original mode and power and HA's saved preferences after stopping;
   verify actual readback. If local writes fail, disable the entry, wait for cloud
   recovery and restore known settings in SolarGo. Do not force invalid zero or
   repeatedly retry. Restore previous automations/clients after verifying final
   state, and report any restoration that could not be confirmed.

Expected setting registers are 10029 (power, tenths of kW) and 10032 (mode).
Stop is 10060=1. No 10000, 10019, 10024, 10025, 10026 or 10157 changes belong to
this experiment. Ordinary polling still opens separate connections; this test
is not a persistent-socket experiment and cannot prove that reconnections are
harmless. Passing the test would support local-control compatibility, not prove
the exact EMS takeover mechanism. A failure during preparation is already useful
and must not be bypassed with unverified writes.

Code validation for this procedure: nine existing Modbus policy tests pass;
installed-pymodbus loopback checks verify the pre-reset/Start sequence, no Start
replay after a lost acknowledgement, and identity fencing. These do not validate
physical behavior on the reporter's wallbox. No physical charging was run here.

### Cloud import-current range warning after returning from Modbus

The cloud `currentLimit` setting concerns the household incoming-current limit for
load management, not the charger's vehicle output-current rating. The unreleased
fix replaces the incorrect 32 A cloud ceiling with the device's SEMS+ range and
0–2000 A defaults when that metadata omits a bound. It preserves a reported 63 A;
it does not clamp the value or write anything when loading the integration.

If valid range metadata contradicts the reported value, or metadata is malformed,
the control is unavailable and explicit writes are rejected. Inspect the original
report and metadata in cached diagnostics. Refresh the entity or reload to retry
discovery. A failed metadata request does not justify assuming the default range.
See `CLOUD_CURRENT_LIMIT.md` for the official application evidence. Direct physical
register equivalence and acceptance on the reporter's model remain unverified.
