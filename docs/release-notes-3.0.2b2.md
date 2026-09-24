Diagnostic beta for issue #21; stable release remains 3.0.1.

### Fix
- If Common/CrossLogin explicitly reports success but returns no token for the expected client, try the original SEMS+ login once.
- Both attempts share the existing timeout, session lock and failed-login cooldown. Credential rejection, throttling and unexpected client identities do not enable this fallback.
- Retain sanitized login diagnostics without passwords, tokens or full responses.

### Verification
- Targeted login and rate-limit regression tests pass, including the reported missing-token response, regional routing, shared deadline and cooldown.
- The missing-token regression fails against 3.0.2b1 and passes with this fix.
- The affected account still needs to confirm whether the original login succeeds.

### Try it
In HACS, open the integration menu, choose Redownload and select 3.0.2b2 (enable beta versions if needed), then restart Home Assistant. With API debug logging enabled, try cloud setup once and share the SEMS login diagnostic lines and related warnings. Do not share credentials or tokens.

This beta excludes the pending Modbus fixes. No production deployment is performed by this release. To roll back, select 3.0.1 in HACS and restart Home Assistant.
