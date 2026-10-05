# Cloud command failures and local recovery

## Meaning of R0305

`R0305` is reported as `remote_control_fail`. It does not by itself distinguish
an unapplied command from a lost/delayed acknowledgement. Earlier #21 evidence
shows positive charging after a single Start returned R0305. In the 2026-10-05
original-HCA experiment, Start returned R0305 without observed positive load
through the subsequent observation window. Never treat either outcome as universal.

The integration reconciles Start/Stop using read-only telemetry. The 3.0.6b1
native recovery additionally permits one local Start after the observation budget,
only when automatic native fallback is enabled and fresh device telemetry proves
an idle, connected, fault-free wallbox. Authentication rejection and invalid
parameters are not evidence of cloud transport failure. Native Socket A support
for the tested original HCA is separate from HCA G2 Modbus TCP support.

## Compound settings

The 3.0.6b2 repair removes the former four-dispatch R0305 settings loop.
A mode/power/energy edit has one uncertain dispatch, not automatic cloud retries.
A definite C0602 authentication rejection may renew the session once because that
particular request was rejected before application.

The "Previous cloud setting is not yet confirmed" guard tracks explicitly sent
fields only. It reads the current cloud configuration before the next compound
edit and clears once every sent field matches. It does not invent telemetry or
replace mismatching companion settings with requested values. Stop is independent
of this guard. Start can still require a mode/power preparation write, which must
respect it. Reload clears the runtime guard but cannot prove the old command was
never applied; it is not a device/backend repair.

## Endpoint investigation, 2026-10-05

The official [GoodWe Swagger](http://www.goodwe-power.com:82/swagger/ui/index)
v3/v4 EV paths and definitions matched the earlier archived contract. No changed
public EV API contract was found. Probes ran in an isolated development HA on
HP840; production cloud ownership was paused and restored. No credentials or
session tokens are included in the public evidence below.

| Route/session | Observed result | What it establishes |
| --- | --- | --- |
| SEMS+ Start, configured original-HCA product model | R0305; no positive power during the observed readback window | Reproduces an uncertain cloud Start; not proof of a universal rejection |
| SEMS+ Stop, same configured session | C0001; load remained zero | Uncertain Stop acknowledgement, not an independent cause of zero load |
| v3 Charging, current web session | 100000 for Start and Stop; no observed load | Not a usable alternative in this experiment |
| v3 Charging, historical semsPlusAndroid session | Login/read succeeded; Start and Stop returned 100000; no observed load | Restoring the old session client did not repair control |
| v3 SetChargeMode | Code 0/data true; v3 briefly echoed PV, then returned to Fast; SEMS+ stayed Fast | A transient configuration echo cannot prove a device mode change |
| v4 SetChargeMode/StopCharging | 100000 with current web session | Neither endpoint established working control |

Early standalone SEMS+ probes omitted productModel and returned R0219. They do
not establish production behavior or the meaning of R0219. Configuring the actual
model reproduced R0305/C0001 instead. A successful read/login is not write validation.
No unverified endpoint fallback is added to the integration.

## Physical development-HA recovery check, 2026-10-05

With production ownership paused, the actual development integration sent one
SEMS+ Start, received R0305 and reconciled for 15 seconds without replaying it.
It then switched to native TCP, observed state 0 / connection 1 / zero load /
fault 0 and sent one local Start. Measured power reached 4.2 kW, about 120 seconds
after the first recorded pending intent. This is an observed end-to-end delay,
not a recovery SLA; cloud response time and the handover still contribute.

The test stopped charging and restored production to its original manual TCP,
Fast and 4.2 kW preference. Final telemetry confirmed zero power, state 0 and
fault 0. Development configuration/registry enablement was restored and the
container stopped. No release, push or production code deployment occurred.

Software validation: all 1,788 unit tests passed on HP840, plus focused fallback,
compound-setting and operation-budget checks and Ruff F checks. The additional
recovery matrix checks disconnected, unknown and ended states using the actual
NativeStatus predicates, not fabricated charging/stopped flags. This original-HCA
physical test does not validate HCA G2 Modbus/PV behavior.

## HCA G2 report #28

The [reporter](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/issues/28#issuecomment-5985541879)
also cannot control the charger using the official SEMS+ web UI. This is evidence
of a device/account/backend problem outside this integration; it does not prove
which component is responsible or that every R0305 has the same cause.

On FW6383, the reporter observes PV charging drop to zero with Modbus enabled,
even when HA is entirely disconnected; disabling Modbus restores PV charging.
The integration cannot repair this by changing polling when no client is present.
Do not automatically write EMS dispatch, grid limits or fabricated PV setpoints.
GoodWe firmware-specific Modbus/EMS documentation and a hardware retest remain
necessary. Original-HCA Socket A validation does not certify this G2 combination.

The [evcc implementation](https://github.com/evcc-io/evcc/blob/master/charger/goodwe.go)
forces Fast during setup and then manages power itself. That is a different
ownership policy, not evidence that device-managed PV works with Modbus enabled.
No code from that separately licensed implementation is copied into this project.
