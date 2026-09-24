# Physical Start/Stop and live power validation, 2026-09-24

All commands used development HA on HP840. Production integration and its two
charging automations were temporarily disabled for exclusive ownership. The car
AC target was raised by the owner. An independent watchdog bounded session time,
missing observations and measured power. Polling configuration was unchanged;
cloud capture observed existing shared-session responses only.

Four final cycles passed with stable measured load before and after each change:

| Transport | Requested limit change | Stable measured load | Stop confirmation |
| --- | --- | --- | --- |
| TCP | 5 -> 6 kW | 4.1 -> 5.5 kW | 6.609 s |
| TCP | 6 -> 4.2 kW | 5.5 -> 4.1 kW | 7.547 s |
| Cloud | 5 -> 6 kW | 4.1 -> 5.5 kW | 11.703 s |
| Cloud | 6 -> 4.2 kW | 5.4 -> 4.1 kW | 23.812 s |

These are observed HA confirmation times, including polling/service latency,
not wire-level switching latency. Cloud validation required advancing device
report timestamps; TCP sampled continuously refreshed native telemetry. Stop
required Off plus zero measured power, not an HTTP acknowledgement alone.
No integration warnings/errors or watchdog intervention occurred in this series.

## Measurement corrections and remaining issue

Earlier 4.2 -> 4.8 kW attempts did not produce a measurable load increase. The
cloud harness also required exact equality of the diagnostic limit. A preliminary
5 kW run compared against the initial charging ramp; its power-change pass is
invalid and is not included in the four results above. It was stopped and
restored before the corrected stable-load series. Do not interpret those
preliminary assertion failures as proven control failures. Requested ceiling,
reported configuration and measured energy flow are separate quantities.

TCP reported the requested limits. Cloud diagnostic `reported_power_limit`
remained 4.2 kW even when two fresh reports showed 5.5 kW after a 6 kW write.
Therefore that cloud field is not a reliable independent acknowledgement of
these writes. Investigate field provenance/freshness and model semantics before
changing its mapping. Do not replace it with HA's desired value or measured load.
The observed lower load at 5 kW does not establish a universal quantization rule.

## Restoration and scope

All test sessions stopped. Fast, desired 4.2 kW, cloud transport, production
integration and both original automations were restored; EMHASS demand was zero.
Temporary capture registration/code was restored and development HA restarted
with its wallbox entry disabled. No production code deployment, push or release.
Only documentation/evidence changed during this physical validation.

HP840 evidence: protocol_research/start_stop_stable_power_20260924, including
summary.json, events.jsonl, samples.jsonl, cloud_trace.json and ha_test.log.
Earlier excluded attempts are retained in start_stop_power_series_20260924,
start_stop_power_tcp_20260924 and start_stop_power_5kw_20260924 for auditability.
Debug logs remain local because API responses may contain private data.

## Cloud limit provenance follow-up

Comparison of saved ordinary API responses shows SEMS+ did reflect writes:

- 5 kW request at epoch 1790269442.8836; first captured SEMS+ detail with
  chargePowerSetted=5 and chargePower=5 at 1790269461.2717 (18.388 s later).
- 6 kW request at 1790269588.6168; first captured detail with both fields=6
  at 1790269682.7394 (94.123 s later). This is an upper observation bound,
  not the propagation delay: no intervening post-write detail was captured.
- Detail still returned 6 at 1790269757.2071. HA at 1790269782.8801 still
  displayed reported limit 4.2, measured load 5.4 and a newer V3 lastUpdate.
  This was 194.263 s after the first 6 kW request; a repeated 6 kW write
  occurred between the two charging cycles. It does not reset the value.
- After the 4.2 kW restoration, detail again returned 4.2.

Code trace: ValueSensor reads coordinator.data[serial][set_charge_power]. In
cloud mode the native coordinator gets that dictionary from
fetch_status_observation -> CloudObservationReader -> V3 GetCurrentChargeinfo.
The reader returns the server dictionary without rewriting that field. The
SEMS+ get_data_gen2 mapping separately maps chargePowerSetted/chargeMaxPower;
that value is not used by this diagnostic sensor on the timestamped route.
Thus SEMS+ had already updated while the displayed V3 field stayed at 4.2.
The capture did not preserve raw V3 set_charge_power in its allowlist, but the
coordinator-to-entity source path and concurrent HA state establish its origin.

Conclusion: do not characterize this as all cloud data taking minutes to update.
The endpoints differ for this field. A still longer V3 lag cannot be ruled out,
nor does SEMS+ configuration alone prove the effective hardware current ceiling.
Use the captured physical load response as independent control evidence. A fix
should explicitly choose a verified configuration source and preserve freshness,
unknown/error behavior and TCP readback, not copy HA's requested preference.
No additional charging, API requests, production changes or runtime edits were
needed for this comparison; only existing evidence and source code were read.

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
