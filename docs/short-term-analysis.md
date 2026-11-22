# 短线精选：模块与运行说明

设计依据见 `docs/superpowers/specs/2026-11-16-pi-agent-short-term-picks-design.md`。

## 结构

两个容器：主服务（FastAPI 单体，单实例单 worker）负责标的池、粗筛、校验、存储、推送与持仓监控；`bajiao-agent`（Node + pi SDK，位于 `agent/`）负责逐只深挖。agent 只加入内网 `bajiao-net`，不发布端口。

```text
币安 tokenized-assets ∩ 现货 exchangeInfo(<代币>USDT, TRADING)
                         ↓
跟踪已有候选（Python）→ Alpaca 批量日线粗筛（≤12 只）
                         ↓
逐只 POST agent /analyze（并发 ≤3，超时 300 s，工具调用 ≤25）
   agent：pi 会话（禁用内置工具）+ OpenBB MCP + Alpaca MCP + 主服务内网 MCP + submit_pick
                         ↓
主服务硬校验（价位、盈亏比、价格偏差、引用、代币可交易性）→ 排序取前 N
                         ↓
SQLite 历史 → Web / 分渠道推送 / 5 个交易日复盘

币安账户 / 代币报价 → 独立持仓风险扫描 → 先通知，再单独补充 agent 持仓复核
```

- `backend/app/services/short_term/`：标的池、因子、粗筛、校验、编排、跟踪、复盘、持仓复核与 agent 客户端。
- `analysis_agent.py`：仅保留给飞书机器人的兼容 OpenAI 对话（`ask_llm`、`analyze_candidate`）。
- `analysis_store.py` / `database.py`：运行、报告、投递和复盘；初始化采用幂等新增列与 WAL，不删除原数据。启动时不写 `system_configs` 默认行。
- `position_monitor.py`：每持仓锁、成功里程碑、定期汇报、独立且可取消的 AI 补充。
- `main.py`：HTTP API、配置重载、内网令牌文件（`data/agent/internal_token`）、可选 Basic 访问控制。
- `agent/`：会话运行器、MCP 桥接、OpenBB 白名单、短线分析 skill，随镜像发布，运行时不下载。
- `static/`：无第三方运行依赖的 UI；设计约定见 `static/DESIGN.md`。

## 数据与历史语义

| 表 | 用途 |
| --- | --- |
| `positions` | 币安实际持仓与提醒偏好；规则修改以 `alert_reset_after_id` 重置提醒水位，不删除旧日志 |
| `alert_logs` | 通知内容、类型及 `success` / `failed` 状态 |
| `system_configs` | 用户配置；敏感值为明文，必须保护文件与备份 |
| `analysis_runs` | 每轮状态、候选数、错误/诊断、调度唯一键 `schedule:{date}:{HH:MM}`、独立推送状态 |
| `daily_picks` | 完整报告 JSON（`strategy: short_term_1w`、目标/止损/盈亏比/有效期、工具调用摘要）、关联运行及隐藏标记；旧 2～4 周记录标记为历史策略，只读、不展示价位、不参与复盘 |
| `analysis_deliveries` | 每运行 / 渠道 / 分段唯一；确认结果、重试次数与下次时间 |
| `analysis_reviews` | 5 个交易日复盘：先触及目标/止损/到期收益、相对 SPY 超额 |

失败运行不覆盖上次有效候选。隐藏不等于删除。启动时将中断运行标为失败；失败任务冷却后可自动重试，成功任务不重复领取；修复配置后也可以手动运行。

价格口径：分析、校验、复盘均为正股 USD（字段 `*_usd`）；代币 USDT 参考价（`*_usdt`）按比例换算，仅作下单参考。候选转持仓仅传代币对，成本、数量、止盈止损需用户独立填写。单个报价缺失时 UI 不计算不完整合计，也不把缺失当成 0。

### 原生股票账户持仓

- 资金账户中的 `EQ_MRVL`、`EQ_TSLA` 等保留原始资产代码，不改名为 `...BUSDT` 代币。账户同步不受八个研究标的限制；研究池不因此扩展，EQ 股票暂不提供 AI 研究映射。
- 股票报价使用币安 `/sapi/v1/equity/market/quote`（API Key，只读），以底层代码请求；USD 买卖中间价仅用于估值与提醒，不是可成交价格。代币仍使用 USDT 现货报价；页面不直接加总 USD 与 USDT。
- 原生股票成交成本优先用 `/sapi/v1/equity/trade/history` 的完整单页（最多 100 条）核对余额，成交价口径 USD。余额不匹配时还会查询 `/sapi/v1/equity/tokenized/history`，仅用明确成功赎回记录的实际比例，衔接前身代币的现货成交、闪兑及后续股票成交，按时间重建移动平均取得成本。用户填写的真实成本优先。
- 转换成本保留原始付款币种（`cost_currency`，如 USDT），不把稳定币金额重新标成 USD。前身代币现货历史从 `fromId=0` 查询，最多 999 条；赎回与股票历史要求无截断。闪兑只取最近 90 天、每段不超过 30 天；任一相关源超限、超出完整覆盖窗口、缺记录、顺序不明或数量不符均拒绝推断。代币扣费计入数量，但其他资产支付的费用不自动折算，因此不是完整含费税务成本。
- `buy_price` 必须与 `cost_currency` 一起读取；阈值仍以 `quote_currency`（EQ 股票 USD）为准。成本与报价币种不同时 API 返回 `pnl_status=currency_mismatch` 且盈亏为 null，页面显示已读原始成本并暂停收益提醒，不做隐含汇率换算。改成本币种必须同时提供该币种真实成本，不自动兑换。
- 未知成本在兼容旧表结构的数据库中用 `buy_price=0`、`cost_source=unknown` 表示，API 返回 `buy_price=null`、`cost_status=unknown`。不以现价冒充成本，不生成默认股票止盈止损，不发送虚假盈亏。
- 原生股票即使报价缺失或金额很小也保留；现货粉尘与无法报价资产在同步响应 `skipped` / `warnings` 中解释。账户读取失败回滚同步，现有设置和历史不删除；手动与自动同步在单实例内串行。

## 主要 API

| 请求 | 作用 |
| --- | --- |
| `GET /api/analysis/status` | 标的池、agent 与 ChatGPT 登录状态、配置就绪、运行状态 |
| `POST /api/analysis/run` | 手动运行，默认不推送；`?push_notifications=true` 显式请求通知 |
| `GET /api/analysis/runs?limit=20` | 历史运行与独立投递状态 |
| `POST /api/analysis/runs/{id}/push` | 发送 / 重试该轮尚未确认成功的渠道分段 |
| `GET /api/analysis/picks` | 最近有效且未隐藏的候选 |
| `DELETE /api/analysis/picks[/{id}]` | 隐藏全部 / 单个候选，不删除历史 |
| `POST /api/analysis/review` | 更新已到期复盘 |
| `GET /api/analysis/reviews`、`/reviews/summary` | 读取复盘与命中率 |
| `POST /api/positions/{id}/analyze` | 仅复核活跃美股代币持仓（agent），不修改提醒规则 |
| `GET /api/positions` | 活跃持仓与币安报价，缺失值返回 null |
| `POST /api/positions` | 新增监控，不交易 |
| `PUT /api/positions/{id}` | 更新监控；显式 null 清空可选止盈/止损价 |
| `DELETE /api/positions/{id}` | 停止监控，不卖出 |
| `POST /api/positions/sync-binance` | 只读同步，失败保留已有持仓 |
| `POST /api/positions/{id}/test-alert` | 实际测试通知，失败不记成功 |
| `GET /api/alerts/logs` | 推送日志 |
| `GET/POST /api/config` | 配置读取 / 保存，敏感字段留空保留 |
| `POST /api/config/test` | 测试数据源、agent 或渠道，通知测试会真实发送 |
| `POST /api/config/binance-credentials` | 当前 Secret 授权、新凭据对验证后原子更新 |
| `GET /api/health` | 不认证的进程健康检查，不验证外部供应商 |

运行状态与投递状态必须分开：例如运行 `completed` + 推送 `failed` 并非送达；`no_candidates` 是正常无机会，不是故障。agent 不可用时该轮记为失败，不降级为旧流程。

## 调度与安全边界

- 每 30 秒扫描风险，与慢速分析分离；每 10 分钟按配置只读同步账户。
- 自动运行默认关闭，启用后按 IANA 时区（默认 Asia/Singapore 09:00、21:00）和本地日期持久化领取任务；每天 agent 会话上限默认 24，超出标记“额度保护跳过”。
- 推送维护每分钟重试未确认投递，复盘维护约每小时执行。外部收取成功与本地记录之间无法消除全部重复风险。
- 设置 `ADMIN_PASSWORD` 后，除 `/api/health` 外 UI/API 均要求 `admin` 的 HTTP Basic 认证。HTTPS、网络隔离与强密码仍是部署方职责，不支持多用户权限模型。
- UI 将 API 文本作为文本节点渲染，来源仅允许 HTTP(S)，外链使用 `noopener noreferrer`；不在浏览器持久化密钥。
- 配置可触发外部数据请求、账户读取或消息发送，不应将控制台开放给不可信用户。Webhook 与模型地址须由可信管理员配置。
- 不自动交易，不承诺供应商持续可用或任何收益；真实 ChatGPT 登录、Alpaca/FRED 权限与通知送达需上线前独立验证。

- agent 的 pi 会话禁用 read/bash/edit/write 等内置工具，只挂自定义工具；Alpaca MCP 仅开 `stock-data,options-data,news` 且为模拟盘模式，不含任何交易能力。内网 MCP 与鉴权接口使用共享令牌，仅 compose 内网可达。
- 不可得数据：期权异动/资金流、做空比例、实时订单流、社交情绪；agent 须如实说明。

## 方法与局限

因子与价位规则见设计文档 §5。方法为有限因子的短线研究，未经收益回测；复盘数据是观察参考，不是含交易成本的策略绩效。
