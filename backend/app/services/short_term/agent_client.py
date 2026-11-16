"""HTTP client for the pi agent container (POST /analyze, GET /health). Never raises on transport errors."""
from dataclasses import dataclass, field
from typing import Callable

import httpx

UNHEALTHY = {'status': 'unreachable', 'auth': 'unknown'}


@dataclass
class AgentResult:
    status: str
    decision: str | None = None
    pick: dict | None = None
    tool_log: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str = ''


def _error(code: str, message: str) -> AgentResult:
    return AgentResult(status='error', error_code=code, error_message=message)


class AgentClient:
    def __init__(self, base_url: str, token_provider: Callable[[], str], timeout_s: float = 330.0):
        self.base_url = base_url.rstrip('/')
        self.token_provider = token_provider
        self.timeout_s = timeout_s

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self.token_provider()}'}

    async def analyze(self, symbol: str, token_symbol: str, purpose: str, context: dict) -> AgentResult:
        payload = {'symbol': symbol, 'token_symbol': token_symbol, 'purpose': purpose, 'context': context}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(f'{self.base_url}/analyze', json=payload, headers=self._headers())
        except httpx.TimeoutException:
            return _error('timeout', 'AI 分析超时')
        except httpx.HTTPError:
            return _error('unreachable', 'AI 分析服务不可达，请检查 agent 容器')
        if resp.status_code != 200:
            return _error(f'http_{resp.status_code}', f'AI 分析服务返回 HTTP {resp.status_code}')
        try:
            body = resp.json()
            status = body['status']
            if status not in ('ok', 'error'):
                raise ValueError('status')
            err = body.get('error') or {}
            return AgentResult(
                status=status, decision=body.get('decision'), pick=body.get('pick'),
                tool_log=list(body.get('tool_log') or []), usage=dict(body.get('usage') or {}),
                error_code=err.get('code') if status == 'error' else None,
                error_message=str(err.get('message') or '') if status == 'error' else '')
        except (ValueError, KeyError, TypeError, AttributeError):
            return _error('bad_response', 'AI 分析服务响应格式无效')

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f'{self.base_url}/health')
            if resp.status_code != 200:
                return dict(UNHEALTHY)
            body = resp.json()
            return body if isinstance(body, dict) else dict(UNHEALTHY)
        except Exception:
            return dict(UNHEALTHY)
