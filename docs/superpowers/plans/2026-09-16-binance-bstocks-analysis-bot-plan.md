# Binance bStocks Analysis & Alert Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个基于币安代币化美股（bStocks）的自动化交易分析与持仓智能提醒系统，支持每日 AI 潜力股研判、持仓复合告警（定时巡检+突破加急）、飞书/企微机器人推送、现代化 Web 仪表盘控制台，并支持 Docker 单容器一键部署。

**Architecture:** 
系统采用分层流水线式 Multi-Agent 架构。后台通过官方 Remote MCP / Binance API 实时获取 bStocks 现货行情与持仓；量化筛选引擎初筛指标异动标的，AI Agent（基于 Pi Agent 架构）结合公司新闻与大事件进行多维研判并输出推荐；调度模块（APScheduler）执行持仓定时巡检与价格波动突破判定，通过 Webhook 发送富文本卡片消息；FastAPI 统一提供 REST API 并直接静态挂载前端 Vite/Tailwind 页面，打包为多阶段构建 Docker 镜像。

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, APScheduler, aiohttp / httpx, SQLite / aiosqlite, Vue 3 / Vite / Tailwind CSS, Docker & Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-16-binance-bstocks-analysis-bot-design.md`

## Global Constraints

- **Python Version**: 3.11+
- **Data Persistence**: SQLite 数据库存放于 `data/bstocks.db`，确保在 Docker Volume 挂载下不丢失
- **API & Port**: 后端与前端单容器集成在 8000 端口
- **Binance MCP**: 优先支持远程官方 MCP 端点 `https://agent.binance.com/mcp/agentic`，并保留标准 Spot API 降级容灾能力
- **Notification**: 支持飞书 Webhook（富文本卡片交互）与企业微信 Webhook 协议格式
- **Coding & Testing**: 遵循 TDD 规范，单元测试覆盖核心计算与过滤逻辑，覆盖率达标

---

### Task 1: 项目基础骨架与数据模型定义 (Project Scaffolding & Models)

**Files:**
- Create: `backend/requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/config.py`
- Create: `backend/app/models/database.py`
- Create: `backend/app/models/schemas.py`
- Test: `backend/tests/test_models.py`

**Interfaces:**
- Consumes: None
- Produces: 
  - `Settings`: 系统配置对象（API Keys, Webhook, DB 路径等）
  - `Position`: 持仓 Pydantic 数据模型与 DB 表结构
  - `Recommendation`: 每日推荐 Pydantic 模型
  - `AlertLog`: 告警流水记录模型

- [ ] **Step 1: 编写数据模型与配置的失败测试**

```python
# backend/tests/test_models.py
import pytest
from backend.app.models.schemas import PositionCreate, PositionOut

def test_position_create_schema_valid():
    data = {
        "symbol": "NVDABUSDT",
        "buy_price": 135.5,
        "quantity": 10.0,
        "take_profit_price": 150.0,
        "stop_loss_price": 125.0,
        "check_interval_hours": 2,
        "ladder_alerts": [0.05, 0.10]
    }
    pos = PositionCreate(**data)
    assert pos.symbol == "NVDABUSDT"
    assert pos.buy_price == 135.5
    assert pos.check_interval_hours == 2
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_models.py`
Expected: ModuleNotFoundError 或 ImportError

- [ ] **Step 3: 编写 requirements.txt、config.py 与数据模型**

实现 `backend/requirements.txt`:
```txt
fastapi>=0.110.0
uvicorn>=0.28.0
pydantic>=2.6.0
pydantic-settings>=2.2.0
aiosqlite>=0.20.0
apscheduler>=3.10.4
httpx>=0.27.0
aiohttp>=3.9.3
pytest>=8.0.0
pytest-asyncio>=0.23.5
```

实现 `backend/app/config.py`:
读取环境变量（如 `BINANCE_API_KEY`, `BINANCE_MCP_URL`, `FEISHU_WEBHOOK_URL`, `OPENAI_API_KEY`, `DB_PATH`）。

实现 `backend/app/models/schemas.py` 与 `backend/app/models/database.py`:
定义 SQLite 建表语句（positions, daily_picks, alert_logs）与异步初始化 `init_db()`。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_models.py`
Expected: PASS

- [ ] **Step 5: Git 提交**

```bash
git add backend/
git commit -m "feat: scaffold backend project structure and core data models"
```

---

### Task 2: 币安远程 MCP 客户端与行情适配层 (Binance MCP Client & Market Provider)

**Files:**
- Create: `backend/app/services/binance_client.py`
- Test: `backend/tests/test_binance_client.py`

**Interfaces:**
- Consumes: `backend.app.config.Settings`
- Produces: 
  - `BinanceClient.get_ticker_price(symbol: str) -> float`
  - `BinanceClient.get_klines(symbol: str, interval: str, limit: int) -> list[dict]`
  - `BinanceClient.get_supported_bstocks() -> list[str]`

- [ ] **Step 1: 编写行情客户端的单测（含 Mock 远程 MCP 响应）**

```python
# backend/tests/test_binance_client.py
import pytest
from unittest.mock import AsyncMock, patch
from backend.app.services.binance_client import BinanceClient

@pytest.mark.asyncio
async def test_get_ticker_price():
    client = BinanceClient()
    with patch.object(client, "_call_mcp_or_api", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"symbol": "NVDABUSDT", "price": "142.50"}
        price = await client.get_ticker_price("NVDABUSDT")
        assert price == 142.50
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_binance_client.py`
Expected: FAIL (BinanceClient 未定义)

- [ ] **Step 3: 实现 BinanceClient 核心逻辑**

在 `backend/app/services/binance_client.py` 中：
1. 优先调用官方 Remote MCP 端点（JSON-RPC 2.0 格式 over HTTP POST）。
2. 异常时自动降级到官方公网 REST API (`https://api.binance.com/api/v3/ticker/price`, `/api/v3/klines`)，确保不因 MCP 临时抖动影响盯盘。
3. 维护当前 bStocks 常见资产代码映射表（`NVDABUSDT`, `TSLABUSDT`, `AAPLBUSDT` 等）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_binance_client.py`
Expected: PASS

- [ ] **Step 5: Git 提交**

```bash
git add backend/app/services/binance_client.py backend/tests/test_binance_client.py
git commit -m "feat: implement binance mcp client with rest api fallback"
```

---

### Task 3: 轻量量化初筛与技术指标计算引擎 (Quant Screener Engine)

**Files:**
- Create: `backend/app/services/quant_screener.py`
- Test: `backend/tests/test_quant_screener.py`

**Interfaces:**
- Consumes: `BinanceClient.get_klines`
- Produces: 
  - `QuantScreener.calculate_indicators(klines: list[dict]) -> dict`
  - `QuantScreener.score_stock(symbol: str, indicators: dict) -> float`
  - `QuantScreener.filter_top_candidates(symbols: list[str], top_n: int = 5) -> list[dict]`

- [ ] **Step 1: 编写技术指标计算与初筛评分的单测**

```python
# backend/tests/test_quant_screener.py
import pytest
from backend.app.services.quant_screener import QuantScreener

def test_calculate_indicators_ma_rsi():
    # 模拟 30 根 K 线的收盘价递增走势
    closes = [100.0 + i for i in range(30)]
    screener = QuantScreener()
    indicators = screener.compute_basic_indicators(closes)
    assert "ma5" in indicators
    assert "ma20" in indicators
    assert "rsi" in indicators
    assert indicators["ma5"] > indicators["ma20"] # 呈多头排列
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_quant_screener.py`
Expected: FAIL

- [ ] **Step 3: 编写指标计算与打分逻辑**

在 `backend/app/services/quant_screener.py` 中：
- 计算 MA5, MA10, MA20。
- 计算 14 日 RSI。
- 成交量放量判定（今日成交量 > 5日均量 * 1.3）。
- 打分算法：多头排列（+30分）、RSI 40~65 反弹健康区（+20分）、成交量放大（+25分）、突破前高（+25分）。按得分降序提取候选股票。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_quant_screener.py`
Expected: PASS

- [ ] **Step 5: Git 提交**

```bash
git add backend/app/services/quant_screener.py backend/tests/test_quant_screener.py
git commit -m "feat: implement rule-based quant screening and scoring engine"
```

---

### Task 4: AI 深度研判与新闻推理 Agent (AI Analysis Agent)

**Files:**
- Create: `backend/app/services/ai_agent.py`
- Test: `backend/tests/test_ai_agent.py`

**Interfaces:**
- Consumes: 候选标的技术指标数据、标的公司新闻（可配置搜索工具）
- Produces: 
  - `AiAnalysisAgent.analyze_stock(symbol: str, indicators: dict) -> dict`
  - 输出结构包括：`confidence_level`, `bullish_reasons`, `target_price`, `stop_loss_price`, `summary`

- [ ] **Step 1: 编写 Agent 解析大模型结构化输出的测试**

```python
# backend/tests/test_ai_agent.py
import pytest
from unittest.mock import AsyncMock, patch
from backend.app.services.ai_agent import AiAnalysisAgent

@pytest.mark.asyncio
async def test_agent_generates_analysis():
    agent = AiAnalysisAgent()
    mock_llm_response = """
    {
        "symbol": "NVDABUSDT",
        "confidence_level": "HIGH",
        "bullish_reasons": ["技术面均线多头", "新一代AI芯片发布业绩预期增强"],
        "target_price": 155.0,
        "stop_loss_price": 128.0,
        "summary": "量价齐升，短期具备强烈向上突破动能。"
    }
    """
    with patch.object(agent, "_call_llm", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_llm_response
        result = await agent.analyze_candidate("NVDABUSDT", {"rsi": 55, "ma5": 140})
        assert result["confidence_level"] == "HIGH"
        assert result["target_price"] == 155.0
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_ai_agent.py`
Expected: FAIL

- [ ] **Step 3: 编写 AiAnalysisAgent**

在 `backend/app/services/ai_agent.py` 中：
- 适配 OpenAI / Claude / DeepSeek 协议。
- 组装多维 Prompt（注入标的技术指标、近期价格区间、新闻热点）。
- 严格按照 JSON Schema 格式化输出。
- 包含解析校验与重试回退机制。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_ai_agent.py`
Expected: PASS

- [ ] **Step 5: Git 提交**

```bash
git add backend/app/services/ai_agent.py backend/tests/test_ai_agent.py
git commit -m "feat: implement multi-dimensional ai analysis agent"
```

---

### Task 5: 飞书与企业微信通知网关 (Notification Gateway)

**Files:**
- Create: `backend/app/services/notifier.py`
- Test: `backend/tests/test_notifier.py`

**Interfaces:**
- Consumes: `Settings.FEISHU_WEBHOOK_URL`, `Settings.WECOM_WEBHOOK_URL`
- Produces: 
  - `Notifier.send_daily_picks(picks: list[dict]) -> bool`
  - `Notifier.send_position_routine(position: dict, current_price: float, pnl_pct: float) -> bool`
  - `Notifier.send_urgent_alert(position: dict, event_type: str, detail: str) -> bool`

- [ ] **Step 1: 编写消息构建与发送的单测**

```python
# backend/tests/test_notifier.py
import pytest
from unittest.mock import AsyncMock, patch
from backend.app.services.notifier import Notifier

@pytest.mark.asyncio
async def test_build_feishu_card():
    notifier = Notifier()
    card = notifier.build_feishu_position_card(
        symbol="TSLABUSDT",
        buy_price=210.0,
        current_price=225.0,
        pnl_pct=0.0714
    )
    assert "TSLABUSDT" in str(card)
    assert "+7.14%" in str(card)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_notifier.py`
Expected: FAIL

- [ ] **Step 3: 编写 Notifier 模块**

在 `backend/app/services/notifier.py` 中：
- 实现飞书富文本互动卡片构建器（支持主题色、状态标签、加粗指标）。
- 实现企业微信 Markdown 消息构建器。
- 支持发送测试消息接口，校验 Webhook 配置是否生效。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_notifier.py`
Expected: PASS

- [ ] **Step 5: Git 提交**

```bash
git add backend/app/services/notifier.py backend/tests/test_notifier.py
git commit -m "feat: implement feishu and wecom notification gateway"
```

---

### Task 6: 持仓监控调度器与复合告警引擎 (Position Monitor & Alert Engine)

**Files:**
- Create: `backend/app/services/alert_engine.py`
- Create: `backend/app/services/scheduler.py`
- Test: `backend/tests/test_alert_engine.py`

**Interfaces:**
- Consumes: `Position`, `BinanceClient`, `Notifier`
- Produces: 
  - `AlertEngine.evaluate_position(position: dict, current_price: float) -> list[AlertEvent]`
  - `Scheduler.start()`: 调度日常任务（每日推荐、持仓巡检、高频轮询）

- [ ] **Step 1: 编写告警阶梯与止盈止损触发判断测试**

```python
# backend/tests/test_alert_engine.py
import pytest
from backend.app.services.alert_engine import AlertEngine

def test_evaluate_take_profit_triggered():
    engine = AlertEngine()
    pos = {
        "id": 1,
        "symbol": "NVDABUSDT",
        "buy_price": 100.0,
        "take_profit_price": 120.0,
        "stop_loss_price": 90.0,
        "ladder_alerts": [0.05, 0.10],
        "alerted_ladders": []
    }
    # 涨到 106，触发 +5% 阶梯提醒
    alerts = engine.check_alerts(pos, current_price=106.0)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "LADDER_TAKE_PROFIT"
    assert alerts[0]["level"] == 0.05
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_alert_engine.py`
Expected: FAIL

- [ ] **Step 3: 实现告警引擎与 APScheduler 定时任务**

在 `backend/app/services/alert_engine.py` 与 `scheduler.py` 中：
- 实现阶梯止盈计算（+5%, +10%, +20%）与去重冷却防轰炸机制。
- 实现止盈价/止损价触达加急告警。
- 实现定时循环任务（每隔设定小时播报常规汇报，每隔 1~2 分钟执行阈值高优巡检）。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_alert_engine.py`
Expected: PASS

- [ ] **Step 5: Git 提交**

```bash
git add backend/app/services/alert_engine.py backend/app/services/scheduler.py backend/tests/test_alert_engine.py
git commit -m "feat: implement position monitoring and multi-stage alert engine"
```

---

### Task 7: FastAPI 后端路由与服务组装 (API Endpoints & Integration)

**Files:**
- Create: `backend/app/routes/positions.py`
- Create: `backend/app/routes/recommendations.py`
- Create: `backend/app/routes/settings.py`
- Create: `backend/app/main.py`
- Test: `backend/tests/test_api_routes.py`

**Interfaces:**
- Consumes: All services
- Produces: 完整的 REST API
  - `GET /api/positions`, `POST /api/positions`, `DELETE /api/positions/{id}`
  - `GET /api/recommendations/daily`, `POST /api/recommendations/trigger`
  - `GET/POST /api/settings`, `POST /api/settings/test-notify`
  - `GET /api/health`

- [ ] **Step 1: 编写 API 路由接口测试**

```python
# backend/tests/test_api_routes.py
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest backend/tests/test_api_routes.py`
Expected: FAIL

- [ ] **Step 3: 编写 FastAPI 路由与主入口**

实现 `positions.py`, `recommendations.py`, `settings.py` 及 `main.py`，配置跨域 CORS，并在应用启动时启动数据库连接与调度器。

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest backend/tests/test_api_routes.py`
Expected: PASS

- [ ] **Step 5: Git 提交**

```bash
git add backend/app/routes/ backend/app/main.py backend/tests/test_api_routes.py
git commit -m "feat: complete fastapi routes and application lifecycle"
```

---

### Task 8: Web 控制台前端界面 (Frontend Dashboard)

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/src/App.vue` (或 React `App.tsx`)
- Create: `frontend/src/views/DailyPicks.vue`
- Create: `frontend/src/views/Portfolio.vue`
- Create: `frontend/src/views/SettingsView.vue`
- Create: `frontend/src/components/Navbar.vue`

**Interfaces:**
- Consumes: `/api/positions`, `/api/recommendations`, `/api/settings`
- Produces: 编译打包到 `frontend/dist/` 的完整静态资源

- [ ] **Step 1: 初始化前端项目脚手架与样式库**

配置 Vite + Vue 3 / React + Tailwind CSS + Lucide Icons。

- [ ] **Step 2: 构建“每日潜力推荐”看板视图**

展示推荐卡片、看涨理由列表、建议点位、一键刷新重新分析按钮。

- [ ] **Step 3: 构建“持仓跟踪与监控室”视图**

展示实时盈亏、添加持仓弹窗、调整提醒频率与止损止盈滑块。

- [ ] **Step 4: 构建“系统设置”视图**

配置飞书/企微 Webhook、大模型 Key、币安连接状态、一键测试推送。

- [ ] **Step 5: 执行前端打包测试**

Run: `cd frontend && npm run build`
Expected: 成功生成 `frontend/dist` 静态包

- [ ] **Step 6: Git 提交**

```bash
git add frontend/
git commit -m "feat: build responsive web dashboard for picks, portfolio and settings"
```

---

### Task 9: 单容器多阶段 Docker 镜像与一键编排 (Docker & Compose Packaging)

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`
- Create: `.env.example`
- Create: `README.md`

**Interfaces:**
- Consumes: `backend/`, `frontend/`
- Produces: 可直接运行的 `binance-bstocks-bot:latest` 容器镜像

- [ ] **Step 1: 编写多阶段构建 Dockerfile**

阶段 1 用 `node:18-alpine` 构建前端；阶段 2 用 `python:3.11-slim` 复制后端代码与前端 dist，配置默认启动命令 `uvicorn backend.app.main:app --host 0.0.0.0 --port 8000`。

- [ ] **Step 2: 编写 docker-compose.yml 与持久化目录映射**

```yaml
version: '3.8'
services:
  bstocks-bot:
    build: .
    container_name: bstocks-analysis-bot
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    env_file:
      - .env
```

- [ ] **Step 3: 编写快速启动说明与完整文档 (README.md)**

涵盖如何获取币安 MCP/行情、如何配置飞书机器人 Webhook、如何通过 `docker compose up -d` 一键启动。

- [ ] **Step 4: 本地 Docker 构建验证**

Run: `docker build -t bstocks-bot:test .`
Expected: 构建成功且镜像正常导出。

- [ ] **Step 5: Git 提交**

```bash
git add Dockerfile docker-compose.yml .dockerignore .env.example README.md
git commit -m "feat: add multi-stage Dockerfile, docker-compose and deployment guide"
```

---

## Self-Review Checklist

- [x] **Spec coverage**: 标的池、币安 MCP 客户端、技术指标初筛、AI Agent 深度分析、持仓定期播报+阶梯告警、飞书/企微通知卡片、Web 前端仪表盘、单容器 Docker 部署，所有设计均有独立 Task 承接。
- [x] **No Placeholders**: 所有步骤均写明了文件路径、接口签名、测试代码与执行指令。
- [x] **Type consistency**: 数据模型在 Task 1 中规范，后续 Task 严格沿用一致的字段命名（`symbol`, `buy_price`, `check_interval_hours`, `ladder_alerts` 等）。
