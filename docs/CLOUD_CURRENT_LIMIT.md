# Cloud currentLimit contract investigation

Status: implemented in the unreleased development tree with simulated tests.
The earlier 0-32 A compatibility lock has been removed. Research evidence below
was collected without device writes; model-specific physical validation remains open.

## Meaning

Fresh official Swagger v3 describes EvChargerDynamicLoad as the input for enabling
dynamic load control and setting the household incoming-current limit. The
currentLimit property itself has no documented minimum/maximum. Swagger v4 exposes
currentLimit as int32, also without numeric bounds.

The current HCA-G2 manual explains dynamic load management in terms of the
household meter and incoming circuit-breaker current (PDF pages 21 and 69 in the
downloaded document). The wallbox reduces or suspends charging to avoid tripping
that breaker. This is not its vehicle output-current rating. A 63 A household
limit therefore does not mean that a 7 kW charger can charge at 63 A.

## Direct official SEMS+ client evidence

Source: https://sems.oss-cn-hongkong.aliyuncs.com/app/semsplus/semsplus.apk
Member: assets/index.android.bundle. Static analysis only, never executed.

Archived member SHA256:
5c98a875f04ddf5fa0b1ae738183be0b0b90c5e5f782fcdd88767c6eb9de91f4
- Module 59365: environment slots 10/11 = 0/1000.
- DynamicLoadManagement 59368: reads currentLimit; number input in A, two decimals.
- Validator 59388 checks numeric finite value against those bounds.
- Submit 59380 sends currentLimit, sn, plantId, productModel through evChargerSetConfig.

The current official APK member differs, so it was downloaded and checked anew.
Current member SHA256:
7fbc7cba885c89e23c744beab9ce01fa3450644915205e9d07bb117ee5a8ec90
- Module 60111: slots 10/11 = 0/2000 (default range).
- DynamicLoadManagement 60114: reads currentLimit and controlItemRanges.
- Exact range key: charge_pile_dynamic_load_import_current_limit.
- Input min/max use that key's min/max, falling back independently to 0/2000
  when missing/null. Unit A, decimalPlaces 2.
- Blur validation 60122 supplies the per-device range to validateAndClamp 60134.
- Submit 60126 validates and sends currentLimit; it still calls the validator
  without explicit range (default 0/2000). Do not replicate this inconsistency:
  integration service validation should enforce the same resolved range as UI.
- API mapping 14751 maps evChargerSetConfig to
  POST /sems-remote/api/ev-charger/set-config, matching our set_config_gen2.
- Existing integration fetch_device_info already reads the corresponding
  GET /sems-remote/api/ev-charger/control-item-content-list/{sn} metadata.

Separate SolarGo local evidence: DynamicLoadManagementActivity.checkParam accepts
0-300 and writes BLE command 2500. This is a different app/protocol/version and
must not define the cloud range. Modbus 10026's documented range remains separate
protocol evidence even though it describes an incoming circuit-breaker limit.

## Implemented behavior

Both cloud entity paths use one range resolver. Valid device metadata overrides the
0–2000 A defaults independently for each bound; only absent/null values use defaults.
Malformed objects, booleans, nonfinite/negative or reversed bounds are rejected.
Unknown reports are never coerced to zero. A report outside the known range makes
the control unavailable, with the unchanged observation retained in diagnostics.
HA omits extra state attributes while unavailable; diagnostic data remains intact.

The UI uses a numeric box with step 0.01 A. Service writes reject excess precision
rather than truncating. Entity identities remain unchanged. Writes read fresh
metadata and device status and reject failed discovery or mismatched identity.
No pending request replaces observed state just because the API acknowledged it.
Metadata is cached per serial, read at discovery and explicit current-limit writes,
not on every telemetry poll. Native configuration refresh retries unresolved ranges
at its existing cadence; legacy explicit update/reload retries discovery.

Tests exercise both cloud entity paths with shared fixtures, fresh range changes,
failed metadata, decimals, boundaries, inconsistent reports and real HA state
serialization. A mocked API transport verifies cache propagation without additional
HTTP requests during telemetry reads. No physical writes were part of these tests.

## Evidence limits

This proves the official client's accepted input scope and API route, not backend
acceptance or physical enforcement for every model. No cloud login, device read,
write, charging, production change, release or GitHub comment was performed.
The reporter's own model-specific controlItemRanges still needs to be collected;
our older original HCA cached metadata contains an empty object. Full read/write
register equivalence has not been measured on the reporter's hardware.

Supporting research files are retained in the HP840 research workspace under
`protocol_research/cloud_current_limit_contract`; they are not bundled with this
repository note. They preserve archived/current decoded functions, current endpoint
mapping, fresh Swagger documents, APK comparison and the manual.
Manual source:
https://en.goodwe.com/Ftp/EN/Downloads/User%20Manual/GW_HCA-G2_User%20Manual-EN.pdf
Swagger sources:
http://www.goodwe-power.com:82/swagger/docs/v3
http://www.goodwe-power.com:82/swagger/docs/v4

## Validation record

HP840 development container, HA 2026.9.2: full suite 1320 passed (210.61 s),
then final affected-module suite 395 passed after five additional edge-case
instances and the transport-change guard. Real HA platform smoke passed for 63 A,
0.01 A steps, decimal mock writes, unchanged observation after acknowledgement,
and contradictory metadata becoming unavailable without changing stored telemetry.
Modbus zero-limit setup/reload and polling smoke checks remained green. Ruff F and
diff whitespace checks passed. No production deployment or physical writes.
