import json
from unittest.mock import AsyncMock
import httpx
import pytest

from backend.app.services.analysis_agent import AnalysisAgent
from backend.app.services.swing_analysis import AnalysisUnavailable


@pytest.mark.asyncio
async def test_swing_agent_loads_actual_skill_instructions_and_bounds_model(monkeypatch):
    agent = AnalysisAgent(api_key='model-secret')
    response = httpx.Response(200, json={'choices':[{'message':{'content':json.dumps({
        'selected':True,'reason':'Observed trend supports a conditional 2–4 week setup.',
        'invalidation':'Daily close below observed support.', 'risks':['Market reversal risk'],
        'source_ids':['history']})}}]})
    post = AsyncMock(return_value=response)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    evidence = {'symbol':'NVDABUSDT','underlying_symbol':'NVDA','sources':[{'id':'history'}]}
    result = await agent.analyze_swing(evidence)
    assert result['selected'] is True
    payload = post.call_args.kwargs['json']
    prompt = payload['messages'][0]['content']
    assert '2' in prompt and '4' in prompt and 'VCP' in prompt and 'CANSLIM' in prompt
    assert '不可信' in prompt
    assert 'model-secret' not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['not json', '{"selected":true}',
    '{"selected":true,"reason":"x","invalidation":"x","risks":[],"source_ids":[]}'])
async def test_invalid_model_output_is_not_a_recommendation(monkeypatch, content):
    agent = AnalysisAgent(api_key='key')
    monkeypatch.setattr(httpx.AsyncClient, 'post', AsyncMock(return_value=httpx.Response(200,
        json={'choices':[{'message':{'content':content}}]})))
    with pytest.raises(AnalysisUnavailable):
        await agent.analyze_swing({'symbol':'NVDABUSDT'})
