"""Shared HTTP compatibility identity for GoodWe cloud endpoints."""

# The SEMS+ login frontend requires a browser-format compatibility header.
# Use the same header for login, telemetry, controls and discovery. This does
# not change token client identities, use browser cookies, or prove reliability.
SEMS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
