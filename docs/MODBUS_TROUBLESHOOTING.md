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

### Cloud import-current range warning after returning from Modbus

The cloud `currentLimit` setting concerns the household incoming-current limit for
load management, not the charger's vehicle output-current rating. The 3.0.3b3 prerelease
fix replaces the incorrect 32 A cloud ceiling with the device's SEMS+ range and
0–2000 A defaults when that metadata omits a bound. It preserves a reported 63 A;
it does not clamp the value or write anything when loading the integration.

If valid range metadata contradicts the reported value, or metadata is malformed,
the control is unavailable and explicit writes are rejected. Inspect the original
report and metadata in cached diagnostics. Refresh the entity or reload to retry
discovery. A failed metadata request does not justify assuming the default range.
See `CLOUD_CURRENT_LIMIT.md` for the official application evidence. Direct physical
register equivalence and acceptance on the reporter's model remain unverified.
