# 3.0.2b1 — Login diagnostics for issue #21

Diagnostic prerelease for [issue #21](https://github.com/prezervos/goodwe-wallbox-sems-home-assistant/issues/21), based on stable **3.0.1**. This adds safe login diagnostics; it is **not a confirmed authentication fix**. Normal authentication decisions, endpoints and retries are unchanged. Unreleased Modbus fixes are not included. Requires Home Assistant 2026.9.2 or newer.

### Install with HACS

1. Open **HACS → GoodWe Wallbox → ⋮ → Redownload**.
2. Open the version selector (sometimes labelled **Need a different version?**) and select **3.0.2b1**. If absent, enable beta/prerelease versions for this repository and refresh its information.
3. Download and **restart Home Assistant**. Keep your existing integration entry; do not delete it.
4. In **Developer tools → Actions**, run:

```yaml
action: logger.set_level
data:
  custom_components.sems_wallbox.sems_api: debug
```

5. Make **one** cloud setup/reconfiguration attempt using the correct credentials. Do not test charging or repeatedly retry login.
6. Open **Settings → System → Logs**, view the full logs, and share lines containing **SEMS login diagnostic**, **SEMS authentication result**, and any login warnings in issue #21. New diagnostic lines do not include passwords, tokens or arbitrary response contents. Redact personal/device identifiers in any other lines you share; do not post full login responses.

### Return to stable

Use the same HACS Redownload/version selector to install **3.0.1**, then restart HA. Turn off beta/prerelease updates if you enabled them for this test. The temporary logger level resets on restart unless you separately configured it in YAML. No configuration migration is introduced.

### Validation

229 focused authentication, rate-limit, telemetry, MQTT and configuration tests passed on the isolated candidate. No real account login or charging command was sent.
