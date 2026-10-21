# 波段工作台：模块与运行说明

## 结构

保留 FastAPI 模块化单体，不拆微服务或复杂 Multi-Agent。静态 UI 和 API 同源，生产只运行一个实例、一个 Uvicorn worker。

```text
原有 8 个币安代币对 ─→ 底层美股映射
                         ↓
FMP 日线 / 财务 / 新闻 / 财报日历
                         ↓
Python 波段规则（MA、ATR、收缩、相对强弱、季度增长）
                         ↓
加载本地 Skills → 模型证据复核 → 结构 / 来源 ID 校验
                         ↓
SQLite 不可破坏的历史报告 → Web / 分渠道分段推送 / 到期回顾

币安账户 / 代币报价 → 独立持仓风险扫描 → 先通知，再单独补充 AI
```

- `us_market_data.py`：FMP stable 只读适配器。校验 OHLCV、排序去重、排除当日未完成日线、拒绝过期数据；规范财务、新闻、事件及来源。上游错误不暴露密钥或响应正文。
- `swing_screener.py`：固定原池映射和 `2～4 周`周期；规则计算和价格层级，不由模型随意生成目标。
- `analysis_skills.py` / `analysis_skills/*.md`：项目适配方法、限制与固定上游版本。Python 实现实际筛选，模型读取方法与证据，不是仅换提示词。
- `analysis_agent.py` / `analysis_errors.py`：模型调用、严格研判结构、可用性和并发错误类型。
- `swing_analysis.py`：单次筛选、持仓风险分析、推送重试及 10/20 个后续交易日回顾。持仓分析缓存 10 分钟，配置重载时清理。
- `analysis_store.py` / `database.py`：运行、报告、投递和回顾；初始化采用幂等新增列与 WAL，不删除原数据。
- `position_monitor.py`：每持仓锁、成功里程碑、定期汇报、独立且可取消的 AI 补充。
- `main.py`：HTTP API、配置重载、可选 Basic 访问控制；监控、账户同步、每日研究、投递/回顾维护分别运行。
- `static/index.html`、`styles.css`、`app.js`：重做后的无第三方运行依赖 UI；设计约定见 `static/DESIGN.md`。

## 数据与历史语义

| 表 | 用途 |
| --- | --- |
| `positions` | 币安实际持仓与提醒偏好；规则修改以 `alert_reset_after_id` 重置提醒水位，不删除旧日志 |
| `alert_logs` | 通知内容、类型及 `success` / `failed` 状态 |
| `system_configs` | 用户配置；敏感值为明文，必须保护文件与备份 |
| `analysis_runs` | 每轮状态、候选数、错误/筛选诊断、调度唯一键、独立推送状态 |
| `daily_picks` | 完整报告 JSON、关联运行及隐藏标记；旧版无价格口径的记录不展示研究价 |
| `analysis_deliveries` | 每运行 / 渠道 / 分段唯一；确认结果、重试次数与下次时间 |
| `analysis_reviews` | 候选后续 10/20 交易日与 SPY 的收盘收益、超额收益 |

失败运行不覆盖上次有效候选。隐藏不等于删除，不能通过页面隐藏破坏历史复盘。启动时将中断运行标为失败；当日调度键保留，失败任务冷却 5 分钟后可自动重试，成功任务不重复领取；修复配置后也可以手动运行。

研究价格口径必须是 `US_EQUITY_USD`；未声明时 UI 隐藏价格。候选转持仓仅传代币对，成本、数量、止盈止损需用户独立填写。单个报价缺失时 UI 不计算不完整合计，也不把缺失当成 0。

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
| `GET /api/analysis/status` | 原池、方法来源、配置就绪、运行状态 |
| `POST /api/analysis/run` | 手动研究，默认不推送；`?push_notifications=true` 显式请求通知 |
| `GET /api/analysis/runs?limit=20` | 历史运行与独立投递状态 |
| `POST /api/analysis/runs/{id}/push` | 发送 / 重试该轮尚未确认成功的渠道分段 |
| `GET /api/analysis/picks` | 最近有效且未隐藏的候选 |
| `DELETE /api/analysis/picks[/{id}]` | 隐藏全部 / 单个候选，不删除历史 |
| `POST /api/analysis/review` | 更新已到期回顾，无足够后续日线则不生成 |
| `GET /api/analysis/reviews` | 读取回顾 |
| `POST /api/positions/{id}/analyze` | 仅复核活跃持仓，不修改提醒规则 |
| `GET /api/positions` | 活跃持仓与币安报价，缺失值返回 null |
| `POST /api/positions` | 新增监控，不交易 |
| `PUT /api/positions/{id}` | 更新监控；显式 null 清空可选止盈/止损价 |
| `DELETE /api/positions/{id}` | 停止监控，不卖出 |
| `POST /api/positions/sync-binance` | 只读同步，失败保留已有持仓 |
| `POST /api/positions/{id}/test-alert` | 实际测试通知，失败不记成功 |
| `GET /api/alerts/logs` | 推送日志 |
| `GET/POST /api/config` | 配置读取 / 保存，敏感字段留空保留 |
| `POST /api/config/test` | 测试数据、模型或渠道，通知测试会真实发送 |
| `POST /api/config/binance-credentials` | 当前 Secret 授权、新凭据对验证后原子更新 |
| `GET /api/health` | 不认证的进程健康检查，不验证外部供应商 |

运行状态与投递状态必须分开：例如研究 `completed` + 推送 `failed` 并非送达；`no_candidates` 是正常无机会，不是模型故障。已确认成功的投递不提供默认重复发送入口。

## 调度与安全边界

- 每 30 秒扫描风险，与慢速研究分离；每 10 分钟按配置只读同步账户。
- 自动研究默认关闭，启用后按 IANA 时区和本地日期持久化领取任务；错过分钟时在同日补跑。纽约时区由 ZoneInfo 处理夏令时。
- 推送维护每分钟重试未确认投递，回顾维护约每小时执行。外部收取成功与本地记录之间无法消除全部重复风险。
- 设置 `ADMIN_PASSWORD` 后，除 `/api/health` 外 UI/API 均要求 `admin` 的 HTTP Basic 认证。HTTPS、网络隔离与强密码仍是部署方职责，不支持多用户权限模型。
- UI 将 API 文本作为文本节点渲染，来源仅允许 HTTP(S)，外链使用 `noopener noreferrer`；不在浏览器持久化密钥。
- 配置可触发外部数据请求、账户读取或消息发送，不应将控制台开放给不可信用户。Webhook 与模型地址须由可信管理员配置。
- 不自动交易，不承诺供应商持续可用或任何收益；真实 FMP 权限、模型兼容与通知送达需上线前独立验证。

## 方法来源

本地适配文档记录许可证、来源与版本：

- `tradermonty/claude-trading-skills`：`cb0f3aadffed6d1089db4b224162c964368baa46`，MIT。
- `anthropics/financial-services`：`574ed3624aebd0418c7e96cd101262f30210ab26`，Apache-2.0。

这里只适配有限因子和研究方法，未完整复制策略实现，未经收益回测。回顾数据是观察参考，不是含交易成本的策略绩效。
