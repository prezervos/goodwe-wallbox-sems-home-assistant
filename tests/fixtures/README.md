# Native protocol fixtures

`hca_idle_cable.json` contains six received physical-device frames: periodic
104 and queried 2104 reports before disconnection, after owner-confirmed
disconnection, and after owner-confirmed reconnection. The device remained idle;
Auto start was disabled. Expectations come from the confirmed cable phases and
zero-load observation, not from calling the production parser.

Private originals and their audit records (line numbers and SHA256 hashes)
remain in the maintainer's external research archive. Public fixtures replace the 22-byte envelope identity and 32-byte serial
field, then recalculate the checksum. Remaining bytes are unchanged. No account
credentials or original device identifiers are included.

These fixtures validate idle cable state on this observed HCA firmware only.
They do not prove charging behavior, energy totals, write support, or other models.
Synthetic loopback tests separately cover fault injection and timing races.
