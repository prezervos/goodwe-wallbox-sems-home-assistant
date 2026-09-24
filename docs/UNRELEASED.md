# Development backlog

Release 3.0.2 consolidates the login, Modbus and native Auto start changes in
[RELEASE_NOTES.md](RELEASE_NOTES.md). Validation scope and hardware limitations
remain documented in [VALIDATION.md](VALIDATION.md) and [NATIVE_TCP.md](NATIVE_TCP.md).

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
