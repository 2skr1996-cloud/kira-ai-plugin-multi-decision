"""Reversible prequeue ownership check for plugin-scoped batch publications."""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Awaitable, Callable

from .reply_media import _contains, _remember, _restore


REPLY_PUBLICATION: ContextVar[Callable[[object], Awaitable[None]] | None] = ContextVar(
    "mdp_reply_publication", default=None,
)


class ReplyPublicationGuard:
    """Capture a scoped callback and await it before forwarding publication.

    The callback identifies and claims the batch. Captured and retained wrappers
    keep their callback checks after close; fresh lookups use the restored method.
    """

    def __init__(self, ctx, logger=None):
        self.ctx = ctx
        self.logger = logger
        self._closed = False
        self._wrappers = []

    def _unavailable(self, reason):
        warning = getattr(self.logger, "warning", None)
        if callable(warning):
            warning(f"[multi-decision] reply publication guard unavailable: {reason}")
        return False

    def install(self) -> bool:
        if self._closed:
            return False
        service = getattr(self.ctx, "event_bus", None)
        previous = getattr(service, "publish", None)
        if not callable(previous):
            return self._unavailable("event_bus.publish is missing")
        if _contains(previous, self):
            return True
        try:
            owned = "publish" in vars(service)
        except TypeError:
            return self._unavailable("event_bus cannot be wrapped reversibly")

        @wraps(previous)
        async def publish(event):
            check = REPLY_PUBLICATION.get()
            if check is not None:
                await check(event)
            return await previous(event)

        _remember(publish, self, previous, owned)
        try:
            service.publish = publish
        except (AttributeError, TypeError) as exc:
            return self._unavailable(f"event_bus cannot be wrapped: {exc}")
        self._wrappers.append((service, publish))
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for service, wrapper in reversed(self._wrappers):
            _restore(service, "publish", wrapper)
        self._wrappers.clear()
