"""Route the pinned SDK to a task-local HTTP client without replacing its defaults."""

from contextvars import ContextVar
from functools import wraps

import httpx


class SDKTransport:
    def __init__(self):
        from bilibili_api.clients.HTTPXClient import HTTPXClient

        self.session = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
        self.client = HTTPXClient(session=self.session)

    def activate(self):
        from bilibili_api.utils import network

        # The SDK keys default sessions only by event loop. Those sessions may retain
        # Set-Cookie headers, so separate Credential objects alone are insufficient.
        # Reuse the dispatcher across plugin reloads; unrelated SDK callers retain
        # the original resolver and their original sessions.
        resolver = network.get_client
        slot = getattr(resolver, "_shixun_client_slot", None)
        if slot is None:
            slot = ContextVar("bilibili_shixun_sdk_client", default=None)

            @wraps(resolver)
            def routed():
                scoped = slot.get()
                return scoped if scoped is not None else resolver()

            routed._shixun_client_slot = slot
            network.get_client = routed
        return slot, slot.set(self.client)

    async def close(self):
        await self.session.aclose()


def sdk_request_context(function):
    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        public = function.__name__ == "search_public" or (
            function.__name__ == "_fetch" and args and args[0].get("kind") == "keyword_search"
        )
        transport = self.public_transport if public else self.transport
        slot, token = transport.activate()
        try:
            return await function(self, *args, **kwargs)
        finally:
            slot.reset(token)

    return wrapped
