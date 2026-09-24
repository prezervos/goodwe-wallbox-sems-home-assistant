"""Read-only reachability checks before releasing a working local connection."""

from __future__ import annotations

import asyncio

from .native_endpoint import check_endpoint


async def async_cloud_preflight(owner):
    """Check the saved device endpoint and account without publishing telemetry.

    Args:
        owner: Native coordinator with an owned endpoint and cloud account.

    Returns:
        True if TCP reachability and a matching authenticated API read succeed.
        An offline or old device report is expected while Socket A is local.

    Raises:
        CloudAuthenticationError: The account needs reauthentication.
        OSError: A connection or timeout prevented the probe.
        ValueError: The saved endpoint cannot be used safely.
    """
    journal = owner.endpoint.journal
    if not journal or owner.cloud is None:
        return False
    endpoint = check_endpoint(journal["original"]).split(",")
    # Use the recorded destination, never a site-specific address or port.
    host, port = endpoint[3], int(endpoint[2])
    async with asyncio.timeout(20):
        async with asyncio.timeout(5):
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
        # This is not a TLS/device login simulation. No wallbox identity or
        # telemetry is sent to its server, and no commands are issued.
        data = await owner.hass.async_add_executor_job(
            owner.cloud.fetch_status_observation, owner.serial
        )
        if not isinstance(data, dict) or data.get("sn") != owner.serial:
            return False
        # Both endpoints share a session, but telemetry access does not prove
        # that the SEMS+ control service is available.
        settings = await owner.hass.async_add_executor_job(
            owner.cloud.get_data_gen2, owner.serial
        )
        return isinstance(settings, dict) and settings.get("sn") == owner.serial
