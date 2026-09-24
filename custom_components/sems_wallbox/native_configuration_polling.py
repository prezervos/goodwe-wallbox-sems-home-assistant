"""Original-HCA configuration readback without cloud substitution."""

import asyncio

from .native_idle_polling import NativeIdlePolling


class NativeConfigurationPolling(NativeIdlePolling):
    """Read configuration while idle or charging without changing Start intent."""

    IDLE_ONLY = False
    INTERVAL = 60
    TASK_NAME = "GoodWe Auto start configuration"
    READ_METHOD = "async_read_configuration"

    def __init__(self, owner):
        super().__init__(owner)
        self.lock = asyncio.Lock()

    @property
    def available(self):
        """Hide values after uncertain delivery or a transport/session change."""
        return super().available and not self.owner.transport._energy_uncertain

    async def tick(self):
        """Serialize reads/publication with writes without invalidating Start."""
        async with self.lock:
            await super().tick()
