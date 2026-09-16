import pytest
import httpx
from unittest.mock import AsyncMock, patch
from backend.app.services.binance_client import BinanceClient

@pytest.mark.asyncio
async def test_get_ticker_price():
    client = BinanceClient()
    with patch.object(client, "_call_mcp_or_api", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"symbol": "NVDABUSDT", "price": "142.50"}
        price = await client.get_ticker_price("NVDABUSDT")
        assert price == 142.50
        mock_call.assert_called_once_with("ticker/price", {"symbol": "NVDABUSDT"})

@pytest.mark.asyncio
async def test_get_ticker_price_invalid_response():
    client = BinanceClient()
    with patch.object(client, "_call_mcp_or_api", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"error": "Invalid symbol"}
        with pytest.raises(ValueError, match="Unexpected price response"):
            await client.get_ticker_price("INVALID")

@pytest.mark.asyncio
async def test_get_klines_list_format():
    client = BinanceClient()
    raw_klines = [
        [
            1710000000000,      # Open time
            "140.00",           # Open
            "145.00",           # High
            "139.50",           # Low
            "144.20",           # Close
            "10000.5",          # Volume
            1710003600000,      # Close time
            "1420000.00",       # Quote asset volume
            500,                # Number of trades
            "5000.0",           # Taker buy base asset volume
            "710000.00",        # Taker buy quote asset volume
            "0"                 # Ignore
        ]
    ]
    with patch.object(client, "_call_mcp_or_api", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = raw_klines
        klines = await client.get_klines("NVDABUSDT", interval="1h", limit=1)
        assert len(klines) == 1
        assert klines[0]["open"] == 140.00
        assert klines[0]["high"] == 145.00
        assert klines[0]["low"] == 139.50
        assert klines[0]["close"] == 144.20
        assert klines[0]["volume"] == 10000.5
        assert klines[0]["timestamp"] == 1710000000000

@pytest.mark.asyncio
async def test_get_klines_dict_format():
    client = BinanceClient()
    dict_klines = [
        {
            "timestamp": 1710000000000,
            "open": 100.0,
            "high": 105.0,
            "low": 98.0,
            "close": 102.0,
            "volume": 500.0
        }
    ]
    with patch.object(client, "_call_mcp_or_api", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = dict_klines
        klines = await client.get_klines("TSLABUSDT", interval="1d", limit=1)
        assert len(klines) == 1
        assert klines[0]["open"] == 100.0
        assert klines[0]["close"] == 102.0

@pytest.mark.asyncio
async def test_get_supported_bstocks():
    client = BinanceClient()
    symbols = client.get_supported_bstocks()
    assert isinstance(symbols, list)
    assert "NVDABUSDT" in symbols
    assert "TSLABUSDT" in symbols
    assert "AAPLBUSDT" in symbols

@pytest.mark.asyncio
async def test_call_mcp_success():
    client = BinanceClient(mcp_url="https://mcp.binance.com/rpc", api_key="secret-key")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"symbol": "NVDABUSDT", "price": "145.00"}
        }
        mock_post.return_value = mock_response

        res = await client._call_remote_mcp("ticker/price", {"symbol": "NVDABUSDT"})
        assert res == {"symbol": "NVDABUSDT", "price": "145.00"}
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://mcp.binance.com/rpc"
        assert kwargs["json"]["method"] == "ticker/price"
        assert kwargs["headers"]["X-MBX-APIKEY"] == "secret-key"

@pytest.mark.asyncio
async def test_call_mcp_error_raises():
    client = BinanceClient(mcp_url="https://mcp.binance.com/rpc")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -1121, "message": "Invalid symbol."}
        }
        mock_post.return_value = mock_response

        with pytest.raises(RuntimeError, match="MCP RPC Error"):
            await client._call_remote_mcp("ticker/price", {"symbol": "UNKNOWN"})

@pytest.mark.asyncio
async def test_call_rest_api():
    client = BinanceClient(api_key="api-key-test")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {"symbol": "NVDABUSDT", "price": "142.50"}
        mock_get.return_value = mock_response

        data = await client._call_rest_api("ticker/price", {"symbol": "NVDABUSDT"})
        assert data == {"symbol": "NVDABUSDT", "price": "142.50"}
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert args[0] == "https://api.binance.com/api/v3/ticker/price"
        assert kwargs["params"] == {"symbol": "NVDABUSDT"}
        assert kwargs["headers"]["X-MBX-APIKEY"] == "api-key-test"

@pytest.mark.asyncio
async def test_call_mcp_fallback_to_rest():
    client = BinanceClient(mcp_url="https://mcp.binance.com/rpc")
    with patch.object(client, "_call_remote_mcp", new_callable=AsyncMock, side_effect=Exception("MCP unavailable")):
        with patch.object(client, "_call_rest_api", new_callable=AsyncMock) as mock_rest:
            mock_rest.return_value = {"symbol": "TSLABUSDT", "price": "220.10"}
            result = await client._call_mcp_or_api("ticker/price", {"symbol": "TSLABUSDT"})
            assert result == {"symbol": "TSLABUSDT", "price": "220.10"}
            mock_rest.assert_called_once()
