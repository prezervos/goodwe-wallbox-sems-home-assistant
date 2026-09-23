"""Propagate command deadlines into synchronous HTTP without orphaning writes."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import partial
import threading
import time


class BudgetCancelled(RuntimeError):
    """A newer command superseded work that must not be retried."""


class OperationBudget:
    """Share one deadline and cancellation signal across a serialized operation."""

    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        self.cancelled = threading.Event()
        self.owner_task = asyncio.current_task()

    def remaining(self):
        """Return the remaining network budget or reject expired/obsolete work."""
        if self.cancelled.is_set():
            raise BudgetCancelled("Operation superseded before another request")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Operation budget expired")
        return remaining


CURRENT_BUDGET = ContextVar("goodwe_operation_budget", default=None)


def request_timeout(default):
    """Bound an individual HTTP timeout by the remaining operation budget."""
    budget = CURRENT_BUDGET.get()
    return min(default, budget.remaining()) if budget is not None else default


def retry_delay(seconds):
    """Wake immediately when Stop supersedes a pending retry."""
    budget = CURRENT_BUDGET.get()
    if budget is None:
        time.sleep(seconds)
        return
    budget.cancelled.wait(min(seconds, budget.remaining()))
    budget.remaining()


@contextmanager
def serialized_request(lock):
    """Do not wait indefinitely behind another shared-client HTTP operation."""
    budget = CURRENT_BUDGET.get()
    if budget is None:
        with lock:
            yield
        return
    while not lock.acquire(timeout=min(0.1, budget.remaining())):
        pass
    try:
        budget.remaining()
        yield
    finally:
        lock.release()


async def async_execute(hass, function, *args):
    """Run HTTP work in HA's executor and drain it before releasing its owner.

    A synchronous request cannot be cancelled by cancelling an asyncio future.
    Preserve serialization until it exits; cancellation prevents subsequent
    requests/retries. The current socket timeout can outlive the async deadline.
    Background polling must not inherit a completed command's budget.
    """
    context = copy_context()
    budget = CURRENT_BUDGET.get()
    if budget is not None and budget.owner_task is not asyncio.current_task():
        context.run(CURRENT_BUDGET.set, None)
        budget = None
    job = asyncio.ensure_future(hass.async_add_executor_job(partial(context.run, function, *args)))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        if budget is not None:
            budget.cancelled.set()
        # A repeated cancellation must not abandon a synchronous device write.
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                continue
            except Exception:
                # The original cancellation wins; retrieve the completed error.
                break
        if not job.cancelled():
            job.exception()
        raise
