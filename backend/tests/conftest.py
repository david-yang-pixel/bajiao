"""Tests must never spend real API credits or send messages using local .env credentials."""
import httpx
import pytest


@pytest.fixture(autouse=True)
def no_unmocked_external_http(monkeypatch):
    original = httpx.AsyncClient._send_single_request

    async def guarded(self, request):
        if isinstance(self._transport_for_url(request.url), httpx.ASGITransport):
            return await original(self, request)
        raise RuntimeError('External HTTP is disabled in tests; mock the provider boundary')

    monkeypatch.setattr(httpx.AsyncClient, '_send_single_request', guarded)
