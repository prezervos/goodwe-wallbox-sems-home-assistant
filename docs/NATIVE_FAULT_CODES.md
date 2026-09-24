# Original-HCA native fault domains

## Reference and validation boundary

Offline analysis on 2026-09-24 used the HCA 3.0 reference image dated
2024-05-28, SHA-256
`1d249b16b1dce9eaa2205eff3b27798906e8c6d196fac7574c26ef791f1da159`.
It is not proof of the installed firmware revision or other GoodWe models.
No device commands, induced faults, charging, or production changes were used.

## Recovered internal/history labels

The labels were compressed in the flash image. Executing the original scatter
loader from `0x08020190` until `0x080202F0` reconstructs initialized SRAM.
The lookup at `0x0803415C` scans 25 slots of 16 bytes at `0x2000006E`:
one code byte followed by a zero-terminated label. The final scanned slot has
code 100 and an empty label; this is not evidence of a meaningful fault code.
`err_max` is retained as a literal firmware marker, not a user-facing diagnosis.

| Internal code | Exact reference label |
| --- | --- |
| 0 | `none` |
| 1 | `auto` |
| 2 | `lack_money` |
| 3 | `curr_less` |
| 4 | `curr_over` |
| 5 | `vol_over` |
| 6 | `vol_less` |
| 7 | `cp_conn_off` |
| 8 | `meter_comm_err` |
| 9 | `card_comm_err` |
| 10 | `stop` |
| 11 | `power_down` |
| 12 | `back_end` |
| 13 | `card_end` |
| 14 | `car_end` |
| 15 | `semiconn_off` |
| 16 | `earth_err` |
| 17 | `leak_curr` |
| 18 | `meter_fault` |
| 19 | `env_temp_over` |
| 20 | `gun_temp_over` |
| 21 | `start_overtime` |
| 22 | `relay_err` |
| 25 | `err_max` |

The actual lookup, strlen and memcpy executed successfully for all 25 scanned
slots; four absent codes (23, 24, 26, 255) left the output empty. These tests
establish reference lookup behavior, not physical fault detection. Codes 23/24
appear in state-machine branches despite having no recovered label.

Previously captured session-history codes 3, 7 and 21 can now be associated with
`curr_less`, `cp_conn_off` and `start_overtime` in this reference. The CP selector
and start-timeout flag research independently agree with these names. History
remains a past-session record; these values are not proof of a current fault.
Several labels describe ordinary termination or business conditions, so do not
classify every nonzero history value as a hardware defect.

## Current TCP status uses a different domain

Do **not** apply the above table to the integration's numeric `fault_code` sensor.
The traced status-update path at `0x0803976C` clears the source field
`0x200113D4 + 0x38`, then sets it to **2** if the condition bitmap at
`0x20007D30 + 0x50` is nonzero. It does not copy the internal/history code.
The builder at `0x0803C340` copies this summary into structure offset `0x2C`;
the serializer at `0x08043430` copies it to TCP status body offset **41**
(frame offset 71), which the integration currently reads.

Executing these three original instruction segments for bitmap values 0, 1,
1024, 262144 and 4294967295 produces wire values 0, 2, 2, 2 and 2 respectively.
This is a controlled data-path test with seeded SRAM and expected entry registers,
not execution of the complete firmware scheduler or every possible writer.
In particular, TCP summary 2 must not be translated as `lack_money`.

## Integration decision and remaining work

Keep raw numeric telemetry, existing identities and control guards unchanged.
Do not infer a detailed diagnosis from the summary field, reuse Gen2 Modbus
bit labels, or expose stale history as a current fault.

A detailed current diagnosis requires tracing the separate condition bitmap
through a verified read path and establishing bit meanings. Command 108 is not
an unmodified bitmap: the reference builder overwrites byte 3 with 1, so it cannot
be decoded naively. Archived current-status reports examined so far contain zero;
nonzero live status and installed-firmware agreement remain unvalidated.

Reproducible private research artifacts on HP840:
`protocol_research/native_fault_labels_20260924/verify_fault_domains.py` and
`results.json` under the existing GoodWe research workspace. The verifier checks
the image hash, executes the original initialization and lookup routines, and
validates the separate status data path. Firmware binaries and private device
captures are intentionally outside the repository.

## Follow-up: detailed command 108 mapping

The reference mapper `0x08038CE4` converts selected bits of the condition word
into a 32-byte report block at `0x200113D4 + 0xD3`. It first clears the block and
sets bit 24 as a marker. **The detailed conditions begin at block byte 4, not
byte 0.** Thus the builder's byte-3 overwrite does not destroy these mapped
conditions; treating the first four bytes as the condition bitmap was wrong.

Executed the complete mapper, builder `0x0803CB98`, and serializer `0x08043948`,
stopping before enqueue/send calls. No hardware helpers were stubbed for this
mapping chain. All 32 one-bit condition inputs, four combined patterns and eight
auxiliary-bit inputs passed. Generated frames have command 108, length 99 and a
valid checksum. The block appears at frame bytes 66–97 (body offset 36).
The existing selector was separately checked with 13 positive inputs, 13 cleared
inputs and 13 disabled caller masks; CP is held in a connected state and its
hardware reads/delays are stubbed as in the original selector harness.

Bit positions below are zero-based within their respective words/blocks.

| Source condition bit | Report block bit | Internal reference label |
| --- | --- | --- |
| 0 | 33 | `stop` |
| 1 | 48 | `vol_over` |
| 2 | 54 | `curr_over` |
| 3 | 49 | `vol_less` |
| 6 | 41 | `env_temp_over` |
| 7 | 42 | `gun_temp_over` |
| 9 | 38 | `earth_err` |
| 10 | Not exported | `start_overtime` |
| 13 | 57 | `meter_comm_err` |
| 14 | 45 | `relay_err` |
| 17 | 66 | `leak_curr` |
| 18 | 67 | `curr_less` |
| 19 | 68 | `meter_fault` |

The mapper exports 21 condition bits in total. Nine exported condition bits
still lack a verified label: block bits 32, 34, 43, 55, 58, 64, 65, 69 and 70.
Block bits 72–76 come from a separate runtime field (`0x20007D30 + 0x74`),
not the condition word; do not label these as faults without further research.
Other set bits must remain visible as unknown instead of being discarded.

Notably, source bit 10 (`start_overtime`) is not exported by this mapper.
The CP-disconnect selector also uses separate CP state rather than a demonstrated
mapped condition bit. An empty detailed report therefore does not imply the
absence of every fault, timeout or stop condition. `stop` is a condition label,
not sufficient evidence of a hardware failure.

An offline decoder now validates frame shape/checksum and the reference marker,
preserves unlabelled/unknown bits, and explicitly reports incomplete coverage.
It passed all 32 firmware-generated fixtures, five malformed-frame cases and an
unknown-bit preservation case. One archived real command-108 frame matched the
zero-condition marker; the sustained capture had no command-108 event. This does
not validate nonzero conditions on the installed firmware or refresh frequency.

Research artifacts: `verify_fault_bitmap.py`, `bitmap_results.json`,
`decode_fault_report.py`, and `archive_results.json` beside the earlier verifier.
No integration runtime or control behavior changed. A future additive cached
TCP diagnostic could retain these reports with age/connection identity, but must
not claim full fault coverage or reuse stale details after transport changes.
Nonzero physical validation remains absent; no faults should be induced merely
to populate this mapping.

## Passive diagnostic export (development)

Downloaded integration diagnostics now include `native.fault_details`. This is
cached observation metadata, not a new entity, poll, alarm or charging guard.
No command-108 acknowledgement, explicit query or extra cloud request is sent.
Reports are accepted only after normal device enrollment and the existing
session-envelope check, then checked for serial, connector 1, length and marker.
Unknown layouts clear older details and increment a bounded-memory rejection
counter without breaking optional telemetry or normal control.

- `reference_mapping`: `original_hca_3_0`, identifying the evidence basis rather
  than claiming the connected device runs that firmware.
- `received`: a valid report exists for this connection; false is missing data,
  not a clean bill of health.
- `age_seconds`: elapsed monotonic time since that report, or null when absent.
  Refresh cadence is unverified, so there is no invented report freshness limit.
- `session_usable`: the normal TCP status freshness test passes and TCP is selected
  outside a transition. This does not establish freshness of individual flags.
- `complete_fault_coverage`: always false for the known reference mapping.
- `last_report`: fixed reference labels, unlabelled condition bit positions,
  separate auxiliary bit positions and unknown set bits. No serial, peer address,
  raw packet or arbitrary device text is exported.
- `rejected_reports`: rejected optional layouts in the current connection epoch.

Disconnect/re-registration clears the cache and counter. Cloud mode and transport
transitions suppress the cached report. Healthy status reports cannot refresh
its age; an old report remains explicitly an old observation even if ordinary
status continues. An empty report replaces previous details without asserting
absence of faults. No numeric sensor, entity identity, notification or control
logic is changed. Labels remain English protocol identifiers in diagnostic JSON;
no new user-facing entity or translated UI text is introduced.
