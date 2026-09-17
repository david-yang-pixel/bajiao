# 🚀 Binance bStocks AI 交易分析与盯盘监控机器人

[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](https://www.docker.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-1.0.0-green.svg)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/Python-3.11+-yellow.svg)](https://www.python.org/)

专为币安代币化美股（**bStocks**，如 `AAPLB`、`TSLAB`、`NVDAB`、`COINB`、`SPYB` 等）打造的全天候（24/7）智能交易分析与持仓复合告警机器人。

---

## ✨ 核心特性

1. **24/7 全天候行情与官方远程 MCP 支持**：
   - 原生对接币安官方托管 Remote MCP (`https://agent.binance.com/mcp/agentic`)，无需本地繁琐部署 MCP 服务。
   - 具备币安公共 REST API 自动故障降级功能，双重保障高可用性。
2. **多维度量化初筛 + AI 深度推理**：
   - **量化初筛**：自动抓取日线 K 线，计算 MA5/10/20 趋势、RSI 超买超卖指标、布林带突破以及 5 日均量放量异动，自动评分筛选潜力股。
   - **AI 研判 Agent**：接入大模型综合新闻面、宏观面与形态，输出看涨评分、核心依据、建议建仓区间、目标止盈价与防守止损位。
3. **持仓监控与复合告警引擎**：
   - **定时间隔巡检**：按自选频率（如每 1 小时、2 小时）自动播报当前价格、最新收益率与浮动盈亏。
   - **异动阶梯冲高 / 止损加急**：首次突破阶梯涨幅（如 +5%、+10%）或触发止损线时，立即高优告警，内置 30 分钟防疲劳冷却机制。
4. **全渠道多模态通知**：
   - 支持**飞书富文本互动卡片**与**企业微信 Markdown 消息**双通道实时推送。
5. **开箱即用 Web 可视化看板**：
   - 内置纯现代化暗黑风格金融仪表盘，支持实时行情看板、持仓管理、告警日志追踪与一键 AI 研判。
6. **极简 Docker 容器化一键部署**：
   - 单容器架构托管 FastAPI 后端与 Web 前端静态界面，卷映射保护数据资产永不丢失。

---

## 🛠️ 快速启动指南

### 方式一：Docker Compose 一键启动（推荐）

1. **克隆项目并复制环境变量文件**：
   ```bash
   cp .env.example .env
   ```
2. **修改配置**（填入你的飞书/企业微信 Webhook 以及 OpenAI/DeepSeek API Key）：
   ```bash
   vim .env
   ```
3. **一行命令启动容器**：
   ```bash
   docker compose up -d --build
   ```
4. **访问 Web 控制台**：
   - 打开浏览器访问：`http://localhost:8000`
   - API 文档地址：`http://localhost:8000/docs`

---

### 方式二：本地 Python 环境运行

1. **创建 Python 虚拟环境并安装依赖**：
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r backend/requirements.txt
   ```
2. **启动服务**：
   ```bash
   uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
   ```
3. **运行完整单元测试套件**：
   ```bash
   pytest backend/tests -v
   ```

---

## 🧭 系统架构与设计

详细架构规范与实施计划见文档：
- [完整系统架构设计规范 (Spec)](docs/superpowers/specs/2026-09-16-binance-bstocks-analysis-bot-design.md)
- [分步实施与测试计划 (Plan)](docs/superpowers/plans/2026-09-16-binance-bstocks-analysis-bot-plan.md)
