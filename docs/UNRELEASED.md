# Development backlog

Release 3.0.2 consolidates the login, Modbus and native Auto start changes in
[RELEASE_NOTES.md](RELEASE_NOTES.md). Validation scope and hardware limitations
remain documented in [VALIDATION.md](VALIDATION.md) and [NATIVE_TCP.md](NATIVE_TCP.md).

## Prepared fixes

### Modbus setup with an invalid initial power limit (#21 follow-up)

A successful first Modbus read could report a zero power-limit register. When
mode restoration was enabled and no power preference existed, optional seeding
raised `Invalid saved power limit` and aborted setup. Skip invalid initial reports
instead of storing them or inventing a replacement. Preserve raw telemetry and
existing saved intent; strict validation of stored preferences and user writes
is unchanged. The same optional initialization helper is used by cloud setup.

Regression evidence: six new invalid-report cases failed before the fix; 167
policy/configuration tests pass after it. The extended real-HA configuration smoke
loads actual Modbus entities from a zero-limit report, verifies no device writes,
and preserves a saved valid preference across reload with the same zero report.
The reporter's log does not include the register value, so hardware confirmation
is still needed. This fix is the 3.0.3b1 prerelease candidate for reporter validation; it is not
in a stable release. No production deployment accompanies this candidate.

## Remaining investigation

- Modbus connection-triggered charging interruptions reported in #16: removing
  unsolicited Stop writes addresses one software cause, not every hardware case.
- Original-HCA cloud Auto start: accepted API writes did not establish working
  hardware control. Keep capability gating; native TCP is the verified path.
- Native Auto start on other models/firmware: do not extrapolate the verified
  original-HCA configuration layout or local command envelope.
- Additional native mappings for extended settings require independent readback
  and safe hardware validation; missing data must remain unknown/unavailable.
- Natural token expiry and continuous MQTT telemetry coverage remain unverified.
  Preserve HTTP fallback and do not treat an MQTT connection as full coverage.
