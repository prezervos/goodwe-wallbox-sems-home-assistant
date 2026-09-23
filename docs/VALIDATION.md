# Validation and known limitations

## Reproducible software validation

The release-preparation pass ran on Python 3.14.5, Home Assistant 2026.9.2,
pymodbus 3.15.0 and aiomqtt 2.5.1. All 980 unit tests passed. Thirteen distinct
real-HA smoke invocations passed using temporary configurations, fake cloud
clients and loopback TCP peers; outbound external access was blocked.

Coverage includes cloud/native setup and unload, reauthentication, entity identities,
translations and icons, Start/Stop services, mode/power preference persistence,
transport handovers, latest intent, lost acknowledgements, Modbus identity checks,
optional energy, MQTT polling and minimum-power guards. Missing/active power must
reject a minimum-power write with a translated error and no outgoing setter call.
One stale smoke fixture omitted measured power; it was corrected and all seven
shared-fixture variants passed again. The runtime guard was not relaxed.

Run commands are in [development](DEVELOPMENT.md) and the CI workflow. CI validates
Python 3.12/3.13 separately; passing 3.14 does not establish those matrix results.
HACS/hassfest results must also be checked on the pull request. The broad existing
pymodbus>=3.0.0 requirement does not mean every allowed version was tested.

## Physical evidence and limits

Original GW11 HCA testing established all-mode native Start/Stop, requested-power
changes, cloud/TCP handover, session energy and idle minimum-power writes. Idle
cable detection was verified with two unplug/replug cycles. Test observations do
not certify every firmware or other model; accepted GW7/GW22 profiles primarily
have protocol coverage.

Cloud returns required approximately 35–43 seconds in observed trials, supporting
the 60-second freshness budget rather than a 30-second promise. Some management
operations timed out; successful samples do not imply guaranteed recovery latency.
A 33.479-hour observation accepted 696 charging-topic MQTT hints and no telemetry-
topic events, so ordinary HTTP polling remains necessary. Natural token expiry
and live broker reauthorization have not been established by simulated tests.

Native session energy resets after Stop. Lifetime energy is a separate idle-polled
counter; a three-minute comparison agreed within about 0.57%, while an earlier
short-session discrepancy remains unexplained. No scaling correction is applied.
Physical factory reset and entirely missed-session accounting remain unverified.

Extended settings without verified native mappings stay unavailable in TCP. Cloud
current/load-setting echoes did not establish an effective hardware limit. SolarGo
Auto start works on the tested device, but a portable verified cloud/native setter
and readback were not found. The integration does not implement PV-surplus control
or an independent-host safety watchdog.
