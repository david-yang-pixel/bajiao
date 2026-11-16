import hmac
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .universe import MarketDataUnavailable

_PROXY_HEADERS = ('x-forwarded-for', 'forwarded', 'x-real-ip')


@dataclass
class InternalDeps:
    token_provider: Callable[[], str]
    factors: Callable[[str], Awaitable[dict]]
    token_quote: Callable[[str], Awaitable[dict]]
    crypto_context: Callable[[], Awaitable[dict]]
    market_context: Callable[[], Awaitable[dict]]
    agent_config: Callable[[], Awaitable[dict]]


class SymbolBody(BaseModel):
    symbol: str = Field(pattern=r'^[A-Z.\-]{1,12}$')


class EmptyBody(BaseModel):
    pass


def build_internal_router(deps: InternalDeps) -> APIRouter:
    async def authorize(request: Request):
        if any(h in request.headers for h in _PROXY_HEADERS):
            raise HTTPException(403, '内网接口不允许经代理访问')
        try:
            scheme, token = request.headers.get('authorization', '').split(' ', 1)
        except ValueError:
            scheme = token = ''
        expected = deps.token_provider() or ''
        if scheme.lower() != 'bearer' or not expected or not hmac.compare_digest(
                token.encode('utf-8'), expected.encode('utf-8')):
            raise HTTPException(401, '内网令牌无效')

    router = APIRouter(prefix='/internal', dependencies=[Depends(authorize)])

    async def run(call):
        try:
            return await call()
        except LookupError as exc:
            raise HTTPException(404, str(exc) or '未知标的')
        except (MarketDataUnavailable, ValueError) as exc:
            return JSONResponse(status_code=503, content={'detail': str(exc)})

    @router.get('/agent-config')
    async def agent_config():
        return await run(deps.agent_config)

    @router.post('/tools/get_factors')
    async def get_factors(body: SymbolBody):
        return await run(lambda: deps.factors(body.symbol))

    @router.post('/tools/get_token_quote')
    async def get_token_quote(body: SymbolBody):
        return await run(lambda: deps.token_quote(body.symbol))

    @router.post('/tools/get_crypto_context')
    async def get_crypto_context(body: EmptyBody):
        return await run(deps.crypto_context)

    @router.post('/tools/get_market_context')
    async def get_market_context(body: EmptyBody):
        return await run(deps.market_context)

    return router
