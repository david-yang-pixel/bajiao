import httpx
import pytest

from backend.app.services.short_term.agent_client import AgentClient

OK_BODY = {'status': 'ok', 'decision': 'selected', 'pick': {'target_usd': 1}, 'tool_log': [{'call_id': '1'}],
           'usage': {'tool_calls': 1, 'duration_ms': 5}, 'error': None}


def make_post(result, calls):
    async def post(self, url, **kw):
        calls.append((url, kw))
        if isinstance(result, Exception):
            raise result
        return result
    return post


def resp(code, body):
    return httpx.Response(code, json=body, request=httpx.Request('POST', 'http://agent'))


def client():
    return AgentClient('http://agent:8001/', lambda: 'tok123')


@pytest.mark.asyncio
async def test_analyze_ok_sends_bearer_and_body(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx.AsyncClient, 'post', make_post(resp(200, OK_BODY), calls))
    r = await client().analyze('NVDA', 'NVDABUSDT', 'pick', {'as_of': 'x'})
    assert (r.status, r.decision, r.pick, r.error_code) == ('ok', 'selected', {'target_usd': 1}, None)
    assert r.tool_log == [{'call_id': '1'}] and r.usage['tool_calls'] == 1
    url, kw = calls[0]
    assert url == 'http://agent:8001/analyze'
    assert kw['headers']['Authorization'] == 'Bearer tok123'
    assert kw['json'] == {'symbol': 'NVDA', 'token_symbol': 'NVDABUSDT', 'purpose': 'pick', 'context': {'as_of': 'x'}}


@pytest.mark.asyncio
async def test_analyze_error_body(monkeypatch):
    body = {**OK_BODY, 'status': 'error', 'decision': None, 'pick': None, 'error': {'code': 'auth_expired', 'message': '登录过期'}}
    monkeypatch.setattr(httpx.AsyncClient, 'post', make_post(resp(200, body), []))
    r = await client().analyze('NVDA', 'NVDABUSDT', 'pick', {})
    assert (r.status, r.error_code, r.error_message) == ('error', 'auth_expired', '登录过期')


@pytest.mark.asyncio
@pytest.mark.parametrize('exc,code,msg', [
    (httpx.ConnectError('x'), 'unreachable', 'AI 分析服务不可达，请检查 agent 容器'),
    (httpx.ReadTimeout('x'), 'timeout', 'AI 分析超时'),
])
async def test_analyze_transport_errors(monkeypatch, exc, code, msg):
    monkeypatch.setattr(httpx.AsyncClient, 'post', make_post(exc, []))
    r = await client().analyze('NVDA', 'NVDABUSDT', 'pick', {})
    assert (r.status, r.error_code, r.error_message) == ('error', code, msg)
    assert r.tool_log == [] and r.pick is None


@pytest.mark.asyncio
async def test_analyze_http_500_and_bad_response(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, 'post', make_post(resp(500, {}), []))
    r = await client().analyze('NVDA', 'NVDABUSDT', 'pick', {})
    assert (r.error_code, r.error_message) == ('http_500', 'AI 分析服务返回 HTTP 500')
    bad = httpx.Response(200, content=b'not json', request=httpx.Request('POST', 'http://agent'))
    monkeypatch.setattr(httpx.AsyncClient, 'post', make_post(bad, []))
    r = await client().analyze('NVDA', 'NVDABUSDT', 'pick', {})
    assert (r.status, r.error_code, r.error_message) == ('error', 'bad_response', 'AI 分析服务响应格式无效')
    monkeypatch.setattr(httpx.AsyncClient, 'post', make_post(resp(200, {'nope': 1}), []))
    assert (await client().analyze('N', 'T', 'pick', {})).error_code == 'bad_response'


@pytest.mark.asyncio
async def test_health(monkeypatch):
    async def ok(self, url, **kw):
        return httpx.Response(200, json={'status': 'ok', 'auth': 'valid', 'revision': 'r1'}, request=httpx.Request('GET', url))

    async def boom(self, url, **kw):
        raise httpx.ConnectError('x')

    async def non200(self, url, **kw):
        return httpx.Response(503, json={}, request=httpx.Request('GET', url))
    monkeypatch.setattr(httpx.AsyncClient, 'get', ok)
    assert (await client().health())['auth'] == 'valid'
    monkeypatch.setattr(httpx.AsyncClient, 'get', boom)
    assert await client().health() == {'status': 'unreachable', 'auth': 'unknown'}
    monkeypatch.setattr(httpx.AsyncClient, 'get', non200)
    assert await client().health() == {'status': 'unreachable', 'auth': 'unknown'}


def test_default_timeout_exceeds_agent_session_deadline():
    # agent deadline: 300 s from request start (MCP startup included) + bounded MCP close; see agent/src/session.ts
    assert AgentClient('http://agent', lambda: 't').timeout_s >= 400
