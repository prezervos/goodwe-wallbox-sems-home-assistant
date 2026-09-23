"""Persist explicit transport preference separately from automatic outage state."""

class ConnectionIntent:
    """Remember manual TCP and a fast-recovery hint without replaying controls."""

    def __init__(self, store):
        self.store = store
        self.manual_tcp = False
        self.automatic_tcp = False
        self.loaded = False

    async def async_load(self):
        """Load once; an absent record preserves the previous cloud default."""
        if self.loaded:
            return
        data = await self.store.async_load() or {}
        self.manual_tcp = data.get("manual_tcp") is True
        self.automatic_tcp = data.get("automatic_tcp") is True and not self.manual_tcp
        self.loaded = True

    async def async_manual(self, local):
        """Persist a user choice, superseding the automatic recovery hint."""
        await self.store.async_save({"manual_tcp": bool(local), "automatic_tcp": False})
        self.manual_tcp = bool(local)
        self.automatic_tcp = False

    async def async_automatic(self, local):
        """Record fallback ownership until fresh cloud recovery is confirmed."""
        if self.manual_tcp or self.automatic_tcp == local:
            return
        await self.store.async_save({"manual_tcp": False, "automatic_tcp": bool(local)})
        self.automatic_tcp = bool(local)
