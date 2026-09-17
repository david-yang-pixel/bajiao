import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.app.services.analysis_agent import AnalysisAgent

@pytest.mark.asyncio
async def test_analysis_agent_fallback():
    # Without API key, should use robust rule-based reasoning
    agent = AnalysisAgent(api_key="")
    result = await agent.analyze_candidate(
        symbol="NVDABUSDT",
        score=75,
        signals=["MA多头排列", "放量突破"],
        metrics={"close": 120.0, "ma5": 118.0, "ma20": 110.0}
    )

    assert result["symbol"] == "NVDABUSDT"
    assert result["target_price"] > 120.0
    assert result["stop_loss_price"] < 120.0
    assert "NVDABUSDT" in result["symbol"]
    assert "MA多头排列" in result["reason"]

@pytest.mark.asyncio
async def test_analysis_agent_llm_success():
    agent = AnalysisAgent(api_key="sk-mock-key")

    mock_llm_resp = {
        "choices": [{
            "message": {
                "content": '{"symbol": "TSLABUSDT", "reason": "放量拉升突破颈线位", "target_price": 250.0, "stop_loss_price": 220.0, "upside_probability": "High"}'
            }
        }]
    }

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_llm_resp
        mock_post.return_value = mock_resp

        result = await agent.analyze_candidate(
            symbol="TSLABUSDT",
            score=80,
            signals=["放量突破"],
            metrics={"close": 230.0}
        )

        assert result["symbol"] == "TSLABUSDT"
        assert result["target_price"] == 250.0
        assert result["stop_loss_price"] == 220.0
        assert "放量拉升" in result["reason"]
