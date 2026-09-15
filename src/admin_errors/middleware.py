"""Stores the current request in a `ContextVar` so `logger.exception()` inside a view carries
request context even without an explicit `request=` kwarg. Optional and not installed by default.

Sync and async capable; asgiref propagates contextvars through `sync_to_async`, so thread-pool
execution of sync views under ASGI keeps the context too.
"""

from __future__ import annotations

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from admin_errors import context


class RequestContextMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self.get_response):
            return self.__acall__(request)
        token = context._current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            context._current_request.reset(token)

    async def __acall__(self, request):
        token = context._current_request.set(request)
        try:
            return await self.get_response(request)
        finally:
            context._current_request.reset(token)
