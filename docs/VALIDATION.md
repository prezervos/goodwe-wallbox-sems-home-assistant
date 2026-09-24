# Validation and known limitations

## Reproducible software validation

The published 3.0.0 release-preparation pass ran on Python 3.14.5, Home Assistant 2026.9.2,
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
HACS/hassfest results must also be checked on the pull request. The 3.0.0 release's
broad pymodbus>=3.0.0 requirement did not establish compatibility with every
allowed version. Release 3.0.1 pins 3.13.1; the existing real-client
loopback wire checks passed with that package in HA 2026.9.2.

Official HA 2026.9.0, 2026.9.2 and 2026.9.3 sources all pin pymodbus3.13.1 in
both the built-in Modbus manifest and package constraints. Their constraints also
accept aiomqtt2.5.1 and supply requests2.34.2. This is dependency compatibility,
not a full runtime test of each HA version. README documents 2026.9.2 as the tested
baseline and production checks on 2026.9.3; earlier versions are not validated.
HACS now declares a minimum HA version of 2026.9.2, matching the tested baseline.
Older HA versions can conflict with this pin (2026.3.0 requires pymodbus3.11.2)
and are not supported by this update.

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
topic events, so ordinary HTTP polling remains necessary. Later live recovery
and post-reconnect charging-event checks are recorded below; they do not establish
natural expiry or continuous telemetry coverage.

Native session energy resets after Stop. Lifetime energy is a separate idle-polled
counter; a three-minute comparison agreed within about 0.57%, while an earlier
short-session discrepancy remains unexplained. No scaling correction is applied.
Physical factory reset and entirely missed-session accounting remain unverified.

Extended settings without verified native mappings stay unavailable in TCP. Cloud
current/load-setting echoes did not establish an effective hardware limit. SolarGo
Auto start has verified native TCP readback and ON/OFF writes on the tested original
HCA, both while idle and during active charging. The 2026-09-24 development-HA
entity test observed 1.7–4.0 kW throughout the setting changes, then confirmed Stop
and restored Auto start OFF, production/cloud ownership and both automations.
Original-HCA cloud control and other model/firmware combinations remain unproven.
The integration does not implement PV-surplus control or an independent-host
safety watchdog.

## Follow-up: natural expiry and short lifetime-counter sessions

The 2026-09-23 focused follow-up passed 183 existing cloud-session/MQTT and energy
checks with external socket connections blocked. This did not establish natural
GoodWe token expiry: historical cached observations lack renewal markers and an
empty warning/error log is not evidence of successful renewal.

The earlier 0.06 versus0.087239kWh discrepancy belongs to the optional lifetime
counter across two short sessions, not the direct session-energy sensor. Separate
power-state integrals were0.064816/0.022422kWh versus counter increments0.05/0.01kWh.
Their timestamps are HA observations, not independent electrical measurements.
Together with the successful longer comparison, these results do not justify a
scale factor or fixed per-session correction. No runtime change was made for these
unresolved physical-evidence boundaries.

## Same-account login experiment

With production isolated, a second process authenticated using the development HA
account while the development integration remained in cloud mode. During the next
150seconds the original integration reported one successful login, zero session
recovery attempts and one MQTT subscription throughout; normal state publication
continued. The second login did not demonstrate invalidation of the first session.
Session reuse and concurrent valid sessions were not distinguished. This experiment
must not be reported as a passed forced-expiry or natural-expiry recovery test.
Production and automations were restored; no charging commands were used.

Credential-free diagnostic counters and their backoff, reconnect and privacy
assertions passed146focused tests. Counters record events, not proof that a connected
MQTT session delivers all required messages. No additional polling is introduced.

## Live session and MQTT recovery follow-up (2026-09-23)

These development-HA tests supersede the inconclusive second-login observation
above. They used the actual cloud and original GW11 HCA, with production ownership
isolated and restored. They are validation evidence, not new cloud behavior.

| Scenario | Observed result | Remaining limit |
| --- | --- | --- |
| Official SEMS+ session logout | Valid read, successful logout, then C0602 with the same token. The API client recovered with exactly one additional login; running development HA also recovered once. | Forced invalidation is not natural expiry; account-wide logout scope is not established. |
| 180-second MQTT network outage | Disconnect detected after about 121 seconds; reconnect and resubscription about 52 seconds after unblock. HTTP coordinator health remained successful and cloud routing stayed selected. | Timing is an observed sample, not a recovery SLA. Cached diagnostics do not count actual HTTP requests. |
| Charging after resubscription | Four accepted charging events triggered four refreshes; subscription count remained two. Observed power 0 -> 4.2 -> 0 kW; polling policy 60 -> 30 -> 60 seconds. | No telemetry-topic events; no wire-level duplicate-message proof. The precautionary watchdog also issued Stop. |

No login storm was observed during either network test. Successful MQTT subscription
alone is insufficient: post-reconnect charging-event delivery was separately verified.
The normal HTTP polling path remains necessary. A live transition from qualified
continuous telemetry to backup polling remains unverified; the real-HA simulated
polling smoke covers that policy without claiming real broker telemetry coverage.

All tests restored production integration/automation ownership and removed temporary
network helpers and routes. No supported production fault-injection service was added.
Private evidence directories on the development worker: token_revocation_confirmed_20260923,
mqtt_network_blackhole_root_20260923, mqtt_reconnect_charging_20260923.

## Unreleased maintenance checkpoint (2026-09-23)

The complete working-tree review covered 22 modified files. All 989 unit tests
passed in 182.75 seconds on HA 2026.9.2 / Python 3.14.5, using pymodbus 3.13.1
and aiomqtt 2.5.1. Existing catalog tests cover the newly completed EN/CS/DE/ES
Start-refusal translations; no duplicate tests were added for that correction.

Three real-HA smoke invocations passed: native runtime (including entity/service
contracts, saved intent and cloud/TCP fallback), audit/Modbus regressions, and
adaptive MQTT polling. Tests used isolated temporary configurations and loopback
peers. The network guard blocked three HA address-autodetection connection attempts
in the native smoke; no external access was permitted. Unit, audit and polling
runs recorded zero blocked attempts. Expected simulated failure logs are negative
cases, not failed tests.

Review found and fixed missing German/Spanish error entries and stale evidence
wording. No additional blocking defect was identified in the unpublished runtime
diff. This is a development checkpoint, not a new release or production deployment;
version remains 3.0.0. Private review and logs: maintenance_checkpoint_20260923.

## HTTP cadence follow-up (2026-09-23)

Extended the existing real-HA adaptive-polling smoke with HTTP-boundary counting
through the actual native-enabled cloud coordinator and API clients. No production
or active development configuration was changed, and no physical commands were
sent. Mock HTTP responses represented idle/charging; all external access was guarded.

Observed idle/charging gaps:59.76s/29.48s for configured60s/30s (HA scheduler rounding).
Six telemetry requests covered initial read, scheduled idle read, charging event,
scheduled charging read, a20-event burst, and scheduled read after push loss.
The burst caused one refresh; repeating its last event ID was ignored. Configuration
was read twice during initial setup/mode observation and not again during these
steady-state scenarios. Cached sessions required no login. No external connection
attempt occurred. This verifies request composition in a controlled runtime, not
actual cloud rate limits or live traffic volume. Older cloud-only entries use a
different read composition; this specific count applies to the native-enabled path.

## MQTT / HTTP concurrency follow-up (2026-09-23)

137 focused cloud-push, shared-session, native-configuration and observation-reader
tests passed, including two new blocked-refresh outcomes in the existing suite.
The real-HA delayed cloud-response smoke and existing native lifecycle smoke also
passed. Late HTTP data did not overwrite newer TCP data; in-flight hints shared
one refresh, unload drained it, and a replacement listener remained independent.
No physical charging or production changes were used. Evidence: mqtt_http_races_20260923.

## Runtime HTTP cooldown maintenance check (unreleased)

The shared SEMS+/v3 HTTP gate was checked with synthetic HTTP429 and HTTP503
responses, numeric/date Retry-After, malformed headers, bounded headerless
backoff, concurrent callers and cancelled operation budgets. Controls and MQTT
discovery cannot bypass the telemetry cooldown; uncertain Start is not replayed.
Service errors are translated and refused optimistic settings are cleared.

On HP840,1027 unit tests passed in183.79s with external networking blocked.
After the final authentication-boundary clarification,142 targeted tests passed.
The real-HA native lifecycle smoke passed all five reload/unload checks; its three
blocked outbound attempts were HA address discovery, not cloud requests. No
physical charging, production configuration changes or live GoodWe load occurred.
This verifies local behavior, not GoodWe account quotas or cross-process locking.

Integrated cooldown recovery also passed in real HA with mocked HTTP/MQTT and
loopback TCP. Cloud-only recovery resumed HTTP after3.008s for Retry-After:3;
automatic TCP fallback/return resumed after3.102s. Both re-established MQTT
subscriptions and processed a new refresh hint. Stale cloud data did not confirm
handover or enable MQTT. No login or device write occurred. Accelerated scheduler
and supervisor delays validate sequencing, not production-duration guarantees.


## Control concurrency and failed-write audit — 2026-09-24

Development fixes cover explicit Start after Stop, typed pre-delivery supersession,
newer Stop preservation after setting errors, independent Modbus charging evidence,
and routine-notification removal. Optimistic write rollback is ownership-aware,
including cancellation, overlapping success/failure, fresh reports during preference
persistence and concurrent mode/power failures. Pending mode presentation does not
replace shared reported data. Protective Stop alerts remain intact.

The final full suite passed 1,074 tests on Python 3.14.5; the focused regression
set passed 327 tests. Independent Astra xhigh review closed the reproduced findings
with no remaining blocker in its reviewed scope. An isolated real-HA native runtime smoke
passed service, handover, reload, preference, entity/translation and protective-alert
checks using fake cloud and loopback peers. No physical hardware or production
changes were part of this audit validation. Independent Astra xhigh reproduction
results are retained in the HP840 protocol-research evidence directory.


## 3.0.1 release candidate — 2026-09-24

Final candidate: 1,079 unit/regression tests passed. Fourteen isolated real-HA
smoke invocations passed on Python 3.14.5, HA 2026.9.2, pymodbus 3.13.1,
aiomqtt 2.5.1 and requests 2.34.2. This includes the thirteen published CI smoke
commands plus the explicit cooldown-recovery invocation now added to CI.

Coverage: cloud/native lifecycle, reauthentication, entity services and contracts,
manual/automatic handovers, persisted mode/power, configuration semantics, Modbus
loopback wire safety, polling/MQTT hints, stale-response fencing, unload/reload and
minimum-power guards. Cooldown recovery covers both cloud-only and automatic TCP
fallback, honors the 3-second simulated Retry-After and resumes MQTT subscriptions
without any simulated device write. The 60/30-second HTTP cadence measured
59.98/29.45 seconds, with twenty distinct hints coalesced into one refresh.

Independent Astra xhigh audit covered all runtime changes, helper modules and
changed runtime tests. One P2 gap in the legacy cloud select's cooldown error
translation was fixed; absent/disabled policy and both initial/reconciliation
writes now have regression coverage. Independent re-review found no unresolved
runtime release blocker. Parent review covered release metadata/docs and smoke
script/CI wiring. These findings and counts do not guarantee absence of all bugs.

All checks used fake cloud responses and loopback peers; no physical charging,
production changes or live GoodWe requests were made at this release checkpoint.
Natural token expiry, unverified model behavior and continuous MQTT telemetry
remain separate research limits. GitHub Python 3.12/3.13 CI, HACS and hassfest must
run on the proposed release commit before publication; these are not claimed as
completed by the local Python 3.14 run.


## 3.0.2 release validation — 2026-09-24

Independent Astra high review examined all changed runtime modules, configuration
flows, translations, documentation and regressions against 3.0.1. Its independent
focused run passed 275 tests. No unresolved runtime blocker was identified.
Follow-up review checked the final release wording and the additional four-case
mixed-storage test. These checks do not guarantee absence of all bugs.

The isolated Python 3.14.5 suite passed 1,173 tests before the final test-only
addition. The additional regression covers both configuration/energy read orders,
shared command serialization, cancellation after send, late-response fencing and
continued status access. It uses an independent loopback peer and real decoders;
it does not contact a wallbox. The complete Auto start file, including all four
new cases, is run separately, and CI runs the resulting 1,177-test suite.

Release checks use the 14 real-HA smoke commands in the CI workflow on HA 2026.9.2
with pinned dependencies and isolated configurations. GitHub Python 3.12/3.13,
real-HA, HACS and hassfest checks must pass before publication. No production HA
changes, live GoodWe authentication or physical charging are part of these release
checks. Earlier physical Auto start evidence remains scoped to original GW11 HCA:
verified ON/OFF in SolarGo and during charging, followed by restoration to OFF.

The issue #21 reporter confirmed alternate original SEMS+ authentication, regional
Australian cloud reads and MQTT connection in the diagnostic beta. The stable
candidate prefers that original endpoint and retains Common/CrossLogin as a
bounded alternate. MQTT connection is not evidence of complete telemetry delivery.
