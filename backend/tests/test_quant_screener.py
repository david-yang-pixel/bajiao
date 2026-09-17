import pytest
import pandas as pd
from unittest.mock import AsyncMock
from backend.app.services.quant_screener import QuantScreener

def generate_mock_klines(count=50, trend="up"):
    klines = []
    base_price = 100.0
    base_vol = 1000.0
    for i in range(count):
        if trend == "up":
            price = base_price + i * 1.5
            vol = base_vol + (i * 50 if i == count - 1 else 0)
        else:
            price = base_price - i * 1.0
            vol = base_vol

        klines.append({
            "timestamp": 1700000000000 + i * 86400000,
            "open": price - 0.5,
            "high": price + 2.0,
            "low": price - 1.0,
            "close": price,
            "volume": vol
        })
    return klines

def test_calculate_indicators():
    screener = QuantScreener()
    klines = generate_mock_klines(count=30, trend="up")
    df = screener.calculate_indicators(klines)

    assert not df.empty
    assert "ma5" in df.columns
    assert "ma10" in df.columns
    assert "ma20" in df.columns
    assert "rsi" in df.columns
    assert "bb_upper" in df.columns
    assert "vol_ma5" in df.columns
    assert df["ma5"].iloc[-1] > df["ma20"].iloc[-1]

def test_score_stock():
    screener = QuantScreener()
    klines = generate_mock_klines(count=50, trend="up")
    df = screener.calculate_indicators(klines)
    result = screener.score_stock(df)

    assert "score" in result
    assert "signals" in result
    assert "metrics" in result
    assert result["score"] >= 50
    assert any("MA" in s for s in result["signals"])

@pytest.mark.asyncio
async def test_screen_candidates():
    screener = QuantScreener()
    mock_client = AsyncMock()
    mock_client.get_supported_bstocks.return_value = ["NVDABUSDT", "TSLABUSDT"]
    mock_client.get_klines.return_value = generate_mock_klines(count=50, trend="up")

    candidates = await screener.screen_candidates(mock_client, min_score=40)
    assert len(candidates) == 2
    assert candidates[0]["symbol"] in ["NVDABUSDT", "TSLABUSDT"]
    assert candidates[0]["score"] >= 40
