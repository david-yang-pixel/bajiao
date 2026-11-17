"""Orchestration of the 1-week short-term pick workflow (prescreen -> pi agent -> hard validation)."""
import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from backend.app.services.analysis_errors import AnalysisBusy, AnalysisUnavailable

from .constants import (AGENT_CONCURRENCY, DAILY_SESSION_LIMIT, PICK_HORIZON_SESSIONS, PRESCREEN_LIMIT,
                        REUSE_MAX_AGE_HOURS, REUSE_PRICE_ATR_MULT, SCHEDULE_TZ)
from .factors import compute_factors
from .messages import build_run_message
from .macro_calendar import events_between
from .prescreen import prescreen
from .universe import MarketDataUnavailable, UniverseCache, get_token_quote
from .validation import to_token_prices, validate_pick

logger = logging.getLogger(__name__)

SECTOR_ETF: dict[str, str] = {
    **dict.fromkeys(('NVDA', 'AMD', 'AVGO', 'TSM', 'INTC', 'MU', 'QCOM', 'AMAT', 'LRCX', 'ASML', 'ARM', 'MRVL'), 'SMH'),
    **dict.fromkeys(('AAPL', 'MSFT', 'ORCL', 'CRM', 'ADBE', 'NOW', 'PLTR', 'CSCO', 'IBM', 'SNOW', 'PANW', 'CRWD'), 'XLK'),
    **dict.fromkeys(('AMZN', 'TSLA', 'HD', 'NKE', 'MCD', 'SBUX', 'LOW', 'BKNG', 'UBER'), 'XLY'),
    **dict.fromkeys(('GOOGL', 'GOOG', 'META', 'NFLX', 'DIS', 'T', 'VZ', 'TMUS', 'SPOT'), 'XLC'),
    **dict.fromkeys(('JPM', 'BAC', 'GS', 'MS', 'WFC', 'V', 'MA', 'COIN', 'HOOD', 'PYPL', 'SCHW', 'BLK'), 'XLF'),
    **dict.fromkeys(('XOM', 'CVX', 'COP', 'OXY', 'SLB'), 'XLE'),
    **dict.fromkeys(('UNH', 'LLY', 'JNJ', 'PFE', 'MRK', 'ABBV', 'TMO', 'ABT', 'MRNA'), 'XLV'),
    **dict.fromkeys(('BA', 'CAT', 'GE', 'HON', 'UPS', 'RTX', 'LMT', 'DE'), 'XLI'),
}
BENCHMARKS = ('SPY', 'QQQ')
AGENT_ERRORS = {
    'auth_missing': 'ChatGPT 凭据缺失，请在配置中心上传 auth.json',
    'auth_expired': 'ChatGPT 凭据已过期，请重新登录并上传 auth.json',
    'timeout': 'AI 分析超时',
    'tool_limit': 'AI 分析工具调用次数超限',
    'no_submission': 'AI 未提交结论',
}
TZ = ZoneInfo(SCHEDULE_TZ)
POSITION_CACHE_TTL = 600
POSITION_PRICE_BASIS = '正股 USD（USDT 为按比例换算参考）'


def expires_after(as_of: str, sessions: int = PICK_HORIZON_SESSIONS) -> str:
    d = date.fromisoformat(as_of)
    while sessions:
        d += timedelta(days=1)
        if d.weekday() < 5:
            sessions -= 1
    return d.isoformat()


def _health_problem(health: dict) -> str | None:
    status, auth = health.get('status'), health.get('auth')
    if status == 'ok' and auth == 'valid':
        return None
    if status == 'ok' and auth in ('missing', 'expired'):
        return f"AI 分析服务不可用：{AGENT_ERRORS['auth_' + auth]}"
    return f"AI 分析服务不可用：agent 服务状态 {status or '未知'}，登录状态 {auth or '未知'}"


def _sources(pick: dict, tool_log: list[dict]) -> list[dict]:
    """Cited tool calls; the summary is what the tool actually returned (tool_log), not the model's wording."""
    by_id = {e.get('call_id'): e for e in tool_log}
    return [{'call_id': c['call_id'], 'tool': by_id[c['call_id']].get('tool'), 'url': c.get('url'),
             'summary': by_id[c['call_id']].get('summary') or ''} for c in pick['citations']]


class ShortTermService:
    session_limit = DAILY_SESSION_LIMIT

    def __init__(self, binance, alpaca, agent, notifier, store, now: Callable[[], datetime] | None = None,
                 config_provider: Callable[[], Awaitable[dict]] | None = None):
        self.binance, self.alpaca, self.agent, self.notifier, self.store = binance, alpaca, agent, notifier, store
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.config_provider = config_provider
        self.universe = UniverseCache(binance)
        self._lock = asyncio.Lock()
        self._push_lock = asyncio.Lock()
        # One limit for every agent call (run sessions and position reviews) = the agent's own MAX_CONCURRENT.
        self._agent_sem = asyncio.Semaphore(AGENT_CONCURRENCY)
        self._bars: dict[str, list] = {}
        self._position_cache: dict[str, tuple[float, dict]] = {}

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    def require_config(self):
        if not self.alpaca.configured:
            raise AnalysisUnavailable('请在配置中心填写 Alpaca API Key/Secret，短线分析需要美股日线行情')
        if not getattr(self.binance, 'api_key', None):
            raise AnalysisUnavailable('请在配置中心填写币安 API Key，短线分析需要币安代币标的池')

    # ---- dependencies exposed to the internal tool API ----
    async def agent_config(self) -> dict:
        return await self.config_provider() if self.config_provider else {}

    async def _ensure_bars(self, symbols):
        missing = [s for s in dict.fromkeys(symbols) if s not in self._bars]
        if missing:
            self._bars.update(await self.alpaca.get_daily_bars(missing))

    def _benchmarks(self) -> dict:
        return {k: self._bars[k] for k in (*BENCHMARKS, *set(SECTOR_ETF.values())) if self._bars.get(k)}

    async def _pairs(self):
        return {p.underlying: p for p in await self.universe.get()}

    async def factors_for(self, symbol: str) -> dict:
        pair = (await self._pairs()).get(symbol)
        if pair is None:
            raise LookupError(f'{symbol} 不在币安代币标的池内')
        etf = SECTOR_ETF.get(symbol)
        await self._ensure_bars([symbol, *BENCHMARKS, *([etf] if etf else [])])
        if not self._bars.get(symbol):
            raise MarketDataUnavailable(f'{symbol} 暂无可用日线数据')
        return compute_factors(self._bars[symbol], self._benchmarks(), etf)

    async def token_quote_for(self, symbol: str) -> dict:
        pair = (await self._pairs()).get(symbol)
        if pair is None:
            raise LookupError(f'{symbol} 不在币安代币标的池内')
        try:
            price = await self.alpaca.get_latest_price(symbol)
        except MarketDataUnavailable:
            price = None
        return await get_token_quote(self.binance, pair, price)

    async def crypto_context(self) -> dict:
        out = {}
        for sym in ('BTCUSDT', 'ETHUSDT'):
            closes = [k['close'] for k in await self.binance.get_klines(sym, '1d', 10)]
            if len(closes) < 6:
                raise MarketDataUnavailable(f'{sym} 日线数据不足')
            out[sym] = {'price': closes[-1], 'return_1d_pct': (closes[-1] / closes[-2] - 1) * 100,
                        'return_5d_pct': (closes[-1] / closes[-6] - 1) * 100}
        return out

    async def market_context(self) -> dict:
        await self._ensure_bars(BENCHMARKS)
        out = {}
        for sym in BENCHMARKS:
            closes = [b['close'] for b in self._bars.get(sym) or []]
            if len(closes) < 21:
                raise MarketDataUnavailable(f'{sym} 日线数据不足')
            out[sym] = {'return_5d_pct': (closes[-1] / closes[-6] - 1) * 100,
                        'return_20d_pct': (closes[-1] / closes[-21] - 1) * 100,
                        'above_ma20': closes[-1] > sum(closes[-20:]) / 20}
        today = self.now().date()
        return {**out, 'events': events_between(today, today + timedelta(days=7))}

    # ---- tracking / review / push ----
    @staticmethod
    def _bar_hit(bars, target, stop):
        """First bar touching stop (stop wins same-bar ties) or target -> (event, level)."""
        for b in bars:
            if b['low'] <= stop:
                return 'stopped', stop
            if b['high'] >= target:
                return 'target_hit', target
        return None

    async def track_active(self) -> list[dict]:
        picks = await self.store.active_short_term_picks()
        if not picks:
            return []
        recorded: list[dict] = []
        try:
            bars = await self.alpaca.get_daily_bars(list(dict.fromkeys(p['underlying_symbol'] for p in picks)))
        except MarketDataUnavailable as exc:
            logger.warning('短线跟踪行情不可用: %s', exc)
            return recorded
        try:
            spot = await self.binance.get_spot_symbols()
        except Exception:
            logger.warning('短线跟踪现货状态不可用', exc_info=True)
            spot = {}
        latest_cache: dict[str, float | None] = {}

        async def record(pick, event, price, detail):
            if await self.store.record_pick_event(pick['id'], event, price, detail):
                recorded.append({'pick_id': pick['id'], 'event': event, 'price_usd': price, 'detail': detail,
                                 'symbol': pick['symbol'], 'underlying_symbol': pick['underlying_symbol']})

        for pick in picks:
            under, target, stop = pick['underlying_symbol'], pick['target_price_usd'], pick['stop_price_usd']
            status = spot.get(pick['symbol'])
            if status is not None and status != 'TRADING':
                await record(pick, 'halted', None, f"{pick['symbol']} 现货状态 {status}")
            after = [b for b in bars.get(under) or [] if b['date'] > pick['as_of']]
            hit = self._bar_hit(after, target, stop)
            if hit:
                await record(pick, hit[0], hit[1], '日线触及止损' if hit[0] == 'stopped' else '日线触及目标')
                continue
            if under not in latest_cache:
                try:
                    latest_cache[under] = await self.alpaca.get_latest_price(under)
                except MarketDataUnavailable:
                    latest_cache[under] = None
            price = latest_cache[under]
            if price is not None and price >= target:
                await record(pick, 'target_hit', price, '最新价达到目标')
            elif price is not None and price <= stop:
                await record(pick, 'stopped', price, '最新价触及止损')
            elif len(after) >= PICK_HORIZON_SESSIONS:
                await record(pick, 'expired', after[-1]['close'], f'已满 {PICK_HORIZON_SESSIONS} 个交易日')
        return recorded

    async def review(self) -> dict:
        h = PICK_HORIZON_SESSIONS
        picks = [p for p in await self.store.list_picks(include_hidden=True, all_runs=True)
                 if p.get('strategy') == 'short_term_1w']
        done = {r['pick_id'] for r in await self.store.rows(
            'SELECT pick_id FROM analysis_reviews WHERE horizon_sessions=?', (h,))}
        pending = [p for p in picks if p['id'] not in done]
        if not pending:
            return {'count': 0, 'errors': [], 'message': '还没有可复盘的短线推荐'}
        try:
            bars = await self.alpaca.get_daily_bars(
                list(dict.fromkeys([*(p['underlying_symbol'] for p in pending), 'SPY'])))
        except MarketDataUnavailable as exc:
            return {'count': 0, 'errors': ['*'], 'message': str(exc)}
        spy = {b['date']: b['close'] for b in bars.get('SPY') or []}
        count, errors = 0, []
        for pick in pending:
            under = pick['underlying_symbol']
            after = [b for b in bars.get(under) or [] if b['date'] > pick['as_of']][:h]
            if len(after) < h:
                continue
            last = after[-1]
            if pick['as_of'] not in spy or last['date'] not in spy:
                errors.append(under)
                continue
            change = (last['close'] / pick['current_price_usd'] - 1) * 100
            market = (spy[last['date']] / spy[pick['as_of']] - 1) * 100
            hit = self._bar_hit(after, pick['target_price_usd'], pick['stop_price_usd'])
            outcome = {'stopped': 'stop', 'target_hit': 'target'}[hit[0]] if hit else 'open'
            count += await self.store.execute(
                '''INSERT OR IGNORE INTO analysis_reviews
                (pick_id,horizon_sessions,as_of,return_pct,benchmark_return_pct,excess_return_pct,outcome)
                VALUES (?,?,?,?,?,?,?)''',
                (pick['id'], h, last['date'], round(change, 4), round(market, 4), round(change - market, 4), outcome))
        return {'count': count, 'errors': errors, 'message': f'基于实际 {h} 个后续交易日收盘价的观察收益；不是含成本交易回测'}

    async def push_run(self, run_id, force=True) -> dict:
        async with self._push_lock:
            run = await self.store.get_run(run_id)
            if not run:
                raise LookupError('分析记录不存在')
            if run['status'] == 'running':
                raise AnalysisBusy('分析尚未完成')
            channels = [n for n, url in [('feishu', self.notifier.feishu_webhook), ('wecom', self.notifier.wecom_webhook)] if url]
            if not channels:
                return {'success': False, 'status': 'failed', 'message': '未配置推送渠道'}
            existing = await self.store.rows('SELECT 1 FROM analysis_deliveries WHERE run_id=? LIMIT 1', (run_id,))
            if not existing:
                new_picks = await self.store.list_picks(run_id, include_hidden=True)
                events = await self.store.unnotified_events()
                tracking = len(await self.store.active_short_term_picks())
                await self.store.queue_deliveries(run_id, channels, build_run_message(run, new_picks, events, tracking))
                await self.store.mark_events_notified([e['id'] for e in events])
            rows = await self.store.rows('SELECT * FROM analysis_deliveries WHERE run_id=? ORDER BY channel,part', (run_id,))
            for delivery in rows:
                if delivery['channel'] not in channels or not await self.store.claim_delivery(delivery['id'], force):
                    continue
                try:
                    success = bool(await self.notifier.send_channel_text(delivery['channel'], delivery['body']))
                except Exception:
                    success = False
                await self.store.delivery_result(delivery, success)
            status = await self.store.update_push_status(run_id)
            return {'success': status == 'success', 'status': status,
                    'message': '所有渠道推送成功' if status == 'success' else '推送未全部成功，已保留失败状态，可配置渠道后重试'}

    async def retry_pushes(self):
        rows = await self.store.rows("SELECT id FROM analysis_runs WHERE push_requested=1 AND push_status IN ('pending','failed','partial') ORDER BY id DESC LIMIT 20")
        for row in rows:
            await self.push_run(row['id'], force=False)

    # ---- position review ----
    async def analyze_position(self, symbol: str, buy_price_usdt: float | None = None,
                               quantity: float | None = None) -> dict | None:
        cached = self._position_cache.get(symbol)
        if cached and time.monotonic() - cached[0] < POSITION_CACHE_TTL:
            return cached[1]
        pair = next((p for p in (await self._pairs()).values() if p.symbol == symbol), None)
        if pair is None:
            raise AnalysisUnavailable('该持仓不在币安美股代币现货标的池内')
        under = pair.underlying
        try:
            f = await self.factors_for(under)
            quote = await self.token_quote_for(under)
        except (LookupError, MarketDataUnavailable) as exc:
            raise AnalysisUnavailable(str(exc))
        if f.get('insufficient_history'):
            raise AnalysisUnavailable('该标的历史数据不足，暂无法分析')
        since = self.now().astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        if await self.store.sessions_started_since(since) >= self.session_limit:
            raise AnalysisUnavailable('今日 AI 分析额度已用完')
        position = {'buy_price_usdt': buy_price_usdt, 'quantity': quantity} if buy_price_usdt is not None or quantity is not None else None
        context = {'as_of': f['as_of'], 'reference_price_usd': f['price'], 'position': position}
        async with self._agent_sem:
            sid = await self.store.start_session(None, under, 'position_review')
            try:
                res = await self.agent.analyze(under, pair.symbol, 'position_review', context)
            except Exception:
                logger.exception('持仓复核 agent 调用异常')
                from .agent_client import AgentResult
                res = AgentResult(status='error', error_code='exception', error_message='AI 分析调用异常')
        message = '' if res.status == 'ok' else (AGENT_ERRORS.get(res.error_code) or res.error_message)
        await self.store.finish_session(
            sid, 'ok' if res.status == 'ok' else 'error', res.decision, f['price'], f['atr14'],
            {'decision': res.decision, 'pick': res.pick, 'tool_log': res.tool_log}, message)
        if res.status != 'ok':
            raise AnalysisUnavailable(message or 'AI 分析未完成')
        pick = res.pick or {}
        errs = validate_pick(pick, res.tool_log, symbol=under, reference_price_usd=f['price'], atr14=f['atr14'],
                             token_quote=quote, purpose='position_review')
        if errs:
            raise AnalysisUnavailable('AI 持仓复核未通过校验：' + '；'.join(errs))
        cur, tgt, stop = pick['current_price'], pick.get('target_price'), pick.get('stop_price')
        sources = _sources(pick, res.tool_log)
        tokens = to_token_prices(quote['mid_usdt'], cur, tgt, stop)
        report = {
            'symbol': pair.symbol, 'underlying_symbol': under, 'as_of': f['as_of'], 'view': pick['view'],
            'reason': pick['rationale'], 'invalidation': pick['invalidation'], 'risks': pick['risks'],
            'sources': sources, 'current_price_usd': cur, 'target_price_usd': tgt, 'stop_price_usd': stop,
            **tokens, 'price_basis': POSITION_PRICE_BASIS, 'target_price': tokens['token_target_usdt'],
            'stop_loss_price': tokens['token_stop_usdt'], 'analysis_mode': 'pi_agent'}
        self._position_cache[symbol] = (time.monotonic(), report)
        return report

    # ---- orchestration ----
    async def _result(self, run_id) -> dict:
        run = await self.store.get_run(run_id)
        picks = await self.store.list_picks(run_id)
        return {'run_id': run_id, 'count': len(picks), 'picks': picks,
                'status': run['status'] if run else 'failed', 'message': run['message'] if run else ''}

    async def run(self, top_n: int = 5, push_notifications: bool = False, run_key: str | None = None) -> dict:
        if not 1 <= top_n <= 5:
            raise ValueError('top_n 必须在 1 到 5 之间')
        self.require_config()
        if self._lock.locked():
            raise AnalysisBusy('已有分析正在运行，请稍后查看结果')
        async with self._lock:
            prev = (await self.store.rows('SELECT id,status,message,push_status FROM analysis_runs WHERE run_key=?', (run_key,)) or [None])[0] if run_key else None
            run_id, claimed = await self.store.begin_run(run_key, push_notifications)
            if not claimed:
                return await self._result(run_id)
            errors, diagnostics, picks = [], [], []
            status, message = 'failed', 'AI 分析或数据未完成，本轮未产生新推荐'
            try:
                try:
                    picks, status, message = await self._analyze(run_id, top_n, errors, diagnostics)
                except MarketDataUnavailable as exc:
                    errors.append({'symbol': '*', 'stage': 'data', 'message': str(exc)})
                if picks:
                    await self.store.insert_short_term_picks(run_id, picks)
                await self.store.finish_run(run_id, status, [], errors, diagnostics, message)
                await self.store.execute('UPDATE analysis_runs SET count=? WHERE id=?', (len(picks), run_id))
            except asyncio.CancelledError:
                await self._abort_run(run_id, [], '分析被中断，可稍后重新运行')
                raise
            except Exception:
                logger.exception('短线分析执行异常')
                msg = '分析执行异常，请检查数据与模型服务'
                await self._abort_run(run_id, [{'symbol': '*', 'stage': 'data', 'message': msg}], msg)
            if push_notifications:
                final = await self.store.get_run(run_id)
                if prev and prev['id'] == run_id and final['status'] == prev['status'] and final['message'] == prev['message']:
                    # Re-claimed slot ended exactly as before: its message was already pushed; do not repeat it.
                    await self.store.execute('UPDATE analysis_runs SET push_status=? WHERE id=?', (prev['push_status'], run_id))
                else:
                    if prev and prev['id'] == run_id:  # the earlier attempt's message is outdated; push this outcome
                        await self.store.execute('DELETE FROM analysis_deliveries WHERE run_id=?', (run_id,))
                    await self.push_run(run_id)
            return await self._result(run_id)

    async def _abort_run(self, run_id, errors, message):
        await self.store.execute(
            "UPDATE agent_sessions SET status='error',error='分析被中断',finished_at=? WHERE run_id=? AND status='running'",
            (datetime.now(timezone.utc).isoformat(), run_id))
        await self.store.finish_run(run_id, 'failed', [], errors, [], message)

    async def _analyze(self, run_id, top_n, errors, diagnostics):
        try:
            await self.track_active()
        except Exception:
            logger.exception('短线持仓跟踪失败')
        self._bars = {}
        pairs = await self._pairs()
        etfs = {SECTOR_ETF[s] for s in pairs if s in SECTOR_ETF}
        await self._ensure_bars([*pairs, *BENCHMARKS, *sorted(etfs)])
        benchmarks = self._benchmarks()
        active = {p.get('underlying_symbol') for p in await self.store.active_short_term_picks()}
        factors = {}
        for sym in pairs:
            if sym in active:
                diagnostics.append({'symbol': sym, 'decision': 'active_pick', 'reason': '已有未结束的短线推荐，不重复分析'})
            elif not self._bars.get(sym):
                errors.append({'symbol': sym, 'stage': 'data', 'message': self.alpaca.last_errors.get(sym) or '无可用日线数据'})
            else:
                factors[sym] = compute_factors(self._bars[sym], benchmarks, SECTOR_ETF.get(sym))
        candidates, filtered = prescreen(factors, PRESCREEN_LIMIT)
        diagnostics.extend(filtered)

        now = self.now()
        since = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        used = await self.store.sessions_started_since(since)
        limit = self.session_limit
        outcomes, pending, quota_skipped = [], [], 0
        for cand in candidates:
            sym = cand['symbol']
            reused = await self._reusable(sym, factors[sym], now)
            if reused:
                diagnostics.append({'symbol': sym, 'decision': 'reused',
                                    'reason': f"复用 {reused['finished_at']} 的分析结论（{reused['decision']}）"})
                stored = reused['result'].get('pick')
                current = {**stored, 'current_price': factors[sym]['price']} if stored else None
                outcomes.append((cand, current, reused['result'].get('tool_log') or [],
                                 reused['decision'] == 'selected'))
            elif used >= limit:
                quota_skipped += 1
                diagnostics.append({'symbol': sym, 'decision': 'quota_skip',
                                    'reason': f'额度保护跳过：今日 AI 会话已达上限 {limit}'})
            else:
                used += 1
                pending.append(cand)

        if candidates and quota_skipped == len(candidates):
            return [], 'failed', f'今日 AI 会话已达上限 {limit}，本轮 {quota_skipped} 只候选全部额度保护跳过'
        if pending:
            problem = _health_problem(await self.agent.health())
            if problem:  # start no session (and burn no quota) against an agent that cannot answer
                errors.append({'symbol': '*', 'stage': 'agent', 'message': problem})
                return [], 'failed', problem

        async def work(cand):
            async with self._agent_sem:
                return await self._agent_session(run_id, cand, factors[cand['symbol']], pairs[cand['symbol']])
        results = await asyncio.gather(*(work(c) for c in pending))
        failures = []
        for cand, res in zip(pending, results):
            sym = cand['symbol']
            if res.status != 'ok':
                failures.append(AGENT_ERRORS.get(res.error_code) or res.error_message)
                errors.append({'symbol': sym, 'stage': 'agent', 'message': failures[-1]})
            elif res.decision == 'selected':
                outcomes.append((cand, res.pick, res.tool_log, True))
            else:
                diagnostics.append({'symbol': sym, 'decision': 'rejected',
                                    'reason': (res.pick or {}).get('rationale') or f'AI 结论：{res.decision}'})

        selected = []
        for cand, pick, tool_log, is_selected in outcomes:
            if not is_selected:
                continue
            sym = cand['symbol']
            try:
                report = await self._validated_report(cand, pick or {}, tool_log, factors[sym], pairs[sym], diagnostics)
            except MarketDataUnavailable as exc:
                failures.append(str(exc))
                errors.append({'symbol': sym, 'stage': 'data', 'message': str(exc)})
                continue
            if report:
                selected.append(report)
        selected.sort(key=lambda r: -r['reward_risk'])
        for r in selected[:top_n]:
            diagnostics.append({'symbol': r['underlying_symbol'], 'decision': 'selected', 'reason': r['reason']})
        for r in selected[top_n:]:
            diagnostics.append({'symbol': r['underlying_symbol'], 'decision': 'rejected', 'reason': f'盈亏比排名超出前 {top_n}'})
        picks = selected[:top_n]
        if failures:
            reason = '；'.join(dict.fromkeys(m for m in failures if m))[:200] or '原因未知'
            status, message = ('partial', f'部分 AI 分析未完成；保留 {len(picks)} 只已验证候选') if picks else \
                ('failed', f'AI 分析或数据未完成，本轮未产生新推荐：{reason}')
        elif picks:
            status, message = 'completed', f'完成短线分析，筛选出 {len(picks)} 只候选'
        else:
            status, message = 'no_candidates', '本轮没有符合条件的短线候选，不凑数'
        return picks, status, message

    async def _reusable(self, symbol, f, now):
        last = await self.store.last_ok_session(symbol, 'pick')
        if not last or not last.get('finished_at') or last.get('price_usd') is None or not last.get('atr14'):
            return None
        finished = datetime.fromisoformat(last['finished_at'])
        if now - finished > timedelta(hours=REUSE_MAX_AGE_HOURS):
            return None
        if abs(f['price'] - last['price_usd']) >= REUSE_PRICE_ATR_MULT * last['atr14']:
            return None
        try:
            if await self.alpaca.count_news_since(symbol, finished) != 0:
                return None
        except MarketDataUnavailable:
            return None
        return last

    async def _agent_session(self, run_id, cand, f, pair):
        sym = cand['symbol']
        try:
            return await self._agent_session_inner(run_id, cand, f, pair)
        except Exception:
            logger.exception('%s 会话记录或调用失败', sym)
            from .agent_client import AgentResult
            return AgentResult(status='error', error_code='session_failed', error_message='AI 分析会话执行失败')

    async def _agent_session_inner(self, run_id, cand, f, pair):
        sym = cand['symbol']
        sid = await self.store.start_session(run_id, sym, 'pick')
        context = {'as_of': f['as_of'], 'reference_price_usd': f['price'], 'position': None}
        try:
            res = await self.agent.analyze(sym, pair.symbol, 'pick', context)
        except Exception:
            logger.exception('agent 调用异常')
            from .agent_client import AgentResult
            res = AgentResult(status='error', error_code='exception', error_message='AI 分析调用异常')
        message = '' if res.status == 'ok' else (AGENT_ERRORS.get(res.error_code) or res.error_message)
        await self.store.finish_session(
            sid, 'ok' if res.status == 'ok' else 'error', res.decision, f['price'], f['atr14'],
            {'decision': res.decision, 'pick': res.pick, 'tool_log': res.tool_log}, message)
        return res

    async def _validated_report(self, cand, pick, tool_log, f, pair, diagnostics):
        sym = cand['symbol']
        quote = await self.token_quote_for(sym)
        errs = validate_pick(pick, tool_log, symbol=sym, reference_price_usd=f['price'], atr14=f['atr14'],
                             token_quote=quote, purpose='pick')
        if errs:
            diagnostics.append({'symbol': sym, 'decision': 'rejected_by_validation', 'reason': '；'.join(errs)})
            return None
        cur, tgt, stop = pick['current_price'], pick['target_price'], pick['stop_price']
        sources = _sources(pick, tool_log)
        r = f.get('returns') or {}
        highlights = {'return_5d_pct': r.get('5d'), 'rsi14': f.get('rsi14'), 'volume_ratio': f.get('volume_ratio'),
                      'breakout_20d': f.get('breakout_20d'), 'ma_aligned': f.get('ma_aligned'),
                      'atr_pct': f.get('atr_pct'), 'prescreen_reasons': cand['reasons']}
        return {
            'symbol': pair.symbol, 'underlying_symbol': sym, 'strategy': 'short_term_1w',
            'as_of': f['as_of'], 'expires_after': expires_after(f['as_of']),
            'current_price_usd': cur, 'target_price_usd': tgt, 'stop_price_usd': stop, 'target_price': tgt,
            'target_pct': (tgt / cur - 1) * 100, 'stop_pct': (stop / cur - 1) * 100,
            'reward_risk': (tgt - cur) / (cur - stop),
            **to_token_prices(quote['mid_usdt'], cur, tgt, stop),
            'reason': pick['rationale'], 'invalidation': pick['invalidation'], 'risks': pick['risks'],
            'factor_highlights': highlights, 'sources': sources, 'prescreen_score': cand['score'],
            'analysis_mode': 'pi_agent',
        }
