"""Attempt preflight with a reversible or retained service-scoped delivery barrier."""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from types import SimpleNamespace
from typing import Awaitable, Callable

from .reply_media import _contains, _remember, _restore


REPLY_ATTEMPT: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "mdp_reply_attempt", default=None,
)
_BARRIER = "_mdp_reply_delivery_barrier"


def _contains_barrier(method, barrier):
    # Standard foreign wrappers expose __wrapped__; our helpers validate their
    # own metadata by identity so functools.wraps copies cannot impersonate us.
    seen = set()
    while method is not None and id(method) not in seen:
        seen.add(id(method))
        if _contains(method, barrier):
            return True
        method = getattr(method, "__wrapped__", None)
    return False


class ReplyDeliveryGuard:
    """Check inherited ownership before and after shared delivery.

    A retained barrier and its ContextVar belong to the service across reloads.
    The newest guard owns its lifecycle; retiring an older handle cannot remove
    it. Default close restores the original method. Captured callbacks remain
    authoritative even in retained wrappers after a reversible close.
    """

    def __init__(self, ctx, logger=None):
        self.ctx = ctx
        self.logger = logger
        self._closed = False
        self._context = REPLY_ATTEMPT
        self._wrappers = []

    def _unavailable(self, reason):
        warning = getattr(self.logger, "warning", None)
        if callable(warning):
            warning(f"[multi-decision] reply delivery guard unavailable: {reason}")
        return False

    def set_check(self, check):
        """Bind this task to the service's stable attempt context (or clear it)."""
        return self._context.set(check)

    def install(self):
        if self._closed:
            return False
        # PluginContext and DefaultIMWorkflow receive this same public service.
        service = getattr(self.ctx, "message_delivery", None)
        previous = getattr(service, "send_message_chain", None)
        if not callable(previous):
            return self._unavailable("message_delivery.send_message_chain is missing")
        try:
            attributes = vars(service)
            owned = "send_message_chain" in attributes
        except TypeError:
            return self._unavailable("message_delivery cannot be wrapped reversibly")
        barrier = attributes.get(_BARRIER)
        if (barrier is not None and not getattr(barrier, "_closed", True)
                and _contains_barrier(previous, barrier)):
            barrier.guard = self
            self._context = barrier.context
            if not any(saved is barrier for _, saved in self._wrappers):
                self._wrappers.append((service, barrier))
            return True

        # Preserve an existing service context even if another plugin replaced
        # the method, so inherited old-task bindings do not disappear on reload.
        context = getattr(barrier, "context", self._context)
        barrier = SimpleNamespace(_closed=False, context=context, guard=self, wrapper=None)

        @wraps(previous)
        async def send_message_chain(*args, **kwargs):
            # Capture the service context, never a reloaded module global.
            check = context.get()
            if check is not None:
                await check()
            try:
                result = await previous(*args, **kwargs)
            except BaseException:
                if check is not None:
                    await check()
                raise
            if check is not None:
                await check()
            return result

        barrier.wrapper = send_message_chain
        _remember(send_message_chain, barrier, previous, owned)
        try:
            service.send_message_chain = send_message_chain
        except (AttributeError, TypeError) as exc:
            return self._unavailable(f"message_delivery cannot be wrapped: {exc}")
        attributes[_BARRIER] = barrier
        self._context = context
        self._wrappers.append((service, barrier))
        return True

    def close(self, *, retain=False):
        if self._closed:
            return
        self._closed = True
        for service, barrier in reversed(self._wrappers):
            if barrier.guard is not self:
                continue  # A newer install took ownership of the same barrier.
            barrier.guard = None
            if retain:
                # Keep its owner live for _live_previous and fresh public lookups.
                continue
            barrier._closed = True
            _restore(service, "send_message_chain", barrier.wrapper)
            attributes = vars(service)
            if attributes.get(_BARRIER) is barrier:
                del attributes[_BARRIER]
        self._wrappers.clear()
