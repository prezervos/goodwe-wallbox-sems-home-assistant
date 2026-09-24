"""Roll back unconfirmed setting presentation after failed or cancelled writes."""

from functools import wraps


class OptimisticWrite:
    """Track one entity write without replacing newer observations or requests."""

    def __init__(self, entity):
        self.entity = entity
        self.attributes = {
            name: getattr(entity, name)
            for name in ("_attr_native_value", "_attr_current_option")
            if hasattr(entity, name)
        }
        self.before = dict(entity.coordinator.data.get(entity.sn) or {})
        self.device = None
        self.written = {}
        self.presented = {}
        previous = getattr(entity, "_optimistic_write", None)
        current = entity.coordinator.data.get(entity.sn)
        if previous is not None and previous.device is current:
            # An overlapping write must not promote its predecessor's optimistic
            # values into the authoritative rollback baseline.
            for key, value in previous.written.items():
                if current.get(key) == value:
                    if key in previous.before:
                        self.before[key] = previous.before[key]
                    else:
                        self.before.pop(key, None)
            for name, value in previous.presented.items():
                if getattr(entity, name) == value and name in previous.attributes:
                    self.attributes[name] = previous.attributes[name]

    def capture_device(self, keys=()):
        """Record presentation and only explicitly owned optimistic data fields."""
        self.device = self.entity.coordinator.data.get(self.entity.sn)
        self.presented = {name: getattr(self.entity, name) for name in self.attributes}
        self.written = {
            key: self.device[key]
            for key in keys
            if self.device is not None and key in self.device
            and self.device[key] != self.before.get(key)
        }

    def rollback(self):
        """Clear this write's presentation while retaining newer reported data."""
        entity = self.entity
        if entity._optimistic_write is not self:
            return
        current = entity.coordinator.data.get(entity.sn)
        if self.device is not None and current is self.device:
            for key, value in self.written.items():
                if current.get(key) == value:
                    if key in self.before:
                        current[key] = self.before[key]
                    else:
                        current.pop(key, None)
        for name in ("_pending_state", "_pending_value", "_pending_mode"):
            if hasattr(entity, name):
                setattr(entity, name, None)
        for name, value in self.attributes.items():
            setattr(entity, name, value)
        entity._handle_coordinator_update()
        entity.coordinator.schedule_delayed_refresh(3.0)


def optimistic_write(function):
    """Clear optimistic UI on any failed write, including cancellation.

    The original exception is propagated. Device writes are never retried.
    """
    @wraps(function)
    async def wrapped(entity, *args, **kwargs):
        state = OptimisticWrite(entity)
        entity._optimistic_write = state
        try:
            return await function(entity, *args, **kwargs)
        except BaseException:
            # Cancellation and executor failures must both release optimistic UI.
            state.rollback()
            raise
        finally:
            if entity._optimistic_write is state:
                entity._optimistic_write = None

    return wrapped
