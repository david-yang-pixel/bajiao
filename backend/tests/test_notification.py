import pytest
from unittest.mock import AsyncMock, patch
from backend.app.services.notification import NotificationService

@pytest.mark.asyncio
async def test_notification_service_daily_pick():
    svc = NotificationService(feishu_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/mock")

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'code': 0}
        mock_post.return_value = mock_resp

        ok = await svc.send_daily_pick_card(
            symbol="NVDABUSDT",
            reason="均线多头突破放量",
            target_price=135.0,
            stop_loss=118.0,
            score=85
        )

        assert ok is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args[1]
        assert "NVDABUSDT" in call_args["json"]["content"]["text"]
        assert "135.00" in call_args["json"]["content"]["text"]

@pytest.mark.asyncio
async def test_notification_service_position_alert():
    svc = NotificationService(wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=mock")

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'errcode': 0}
        mock_post.return_value = mock_resp

        ok = await svc.send_position_alert(
            symbol="TSLABUSDT",
            buy_price=200.0,
            curr_price=220.0,
            pnl_pct=10.0,
            alert_type="ladder",
            extra_note="已达到+10%第一止盈位"
        )

        assert ok is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args[1]
        assert "TSLABUSDT" in call_args["json"]["text"]["content"]
        assert "+10.00%" in call_args["json"]["text"]["content"]
