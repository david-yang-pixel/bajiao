"""Orchestration of the 1-week short-term pick workflow (prescreen -> pi agent -> hard validation)."""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from backend.app.services.analysis_errors import AnalysisBusy, AnalysisUnavailable

from .constants import (AGENT_CONCURRENCY, DAILY_SESSION_LIMIT, PICK_HORIZON_SESSIONS, PRESCREEN_LIMIT,
                        REUSE_MAX_AGE_HOURS, REUSE_PRICE_ATR_MULT, SCHEDULE_TZ)
from .factors import compute_factors
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


def expires_after(as_of: str, sessions: int = PICK_HORIZON_SESSIONS) -> str:
    d = date.fromisoformat(as_of)
    while sessions:
        d += timedelta(days=1)
        if d.weekday() < 5:
            sessions -= 1
    return d.isoformat()


class ShortTermService:
    session_limit = DAILY_SESSION_LIMIT

    def __init__(self, binance, alpaca, agent, notifier, store, now: Callable[[], datetime] | None = None,
                 config_provider: Callable[[], Awaitable[dict]] | None = None):
        self.binance, self.alpaca, self.agent, self.notifier, self.store = binance, alpaca, agent, notifier, store
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.config_provider = config_provider
        self.universe = UniverseCache(binance)
        self._lock = asyncio.Lock()
        self._bars: dict[str, list] = {}

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

    # ---- Task 10 placeholders ----
    async def track_active(self) -> list[dict]:
        return []

    async def push_run(self, run_id, force=True) -> dict:
        return {'success': False, 'status': 'not_implemented'}

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
        sem = asyncio.Semaphore(AGENT_CONCURRENCY)
        outcomes, pending = [], []
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
                diagnostics.append({'symbol': sym, 'decision': 'quota_skip', 'reason': f'今日 AI 会话已达上限 {limit}'})
            else:
                used += 1
                pending.append(cand)

        async def work(cand):
            async with sem:
                return await self._agent_session(run_id, cand, factors[cand['symbol']], pairs[cand['symbol']])
        results = await asyncio.gather(*(work(c) for c in pending))
        had_error = False
        for cand, res in zip(pending, results):
            sym = cand['symbol']
            if res.status != 'ok':
                had_error = True
                errors.append({'symbol': sym, 'stage': 'agent', 'message': AGENT_ERRORS.get(res.error_code) or res.error_message})
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
                had_error = True
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
        if had_error:
            status, message = ('partial', f'部分 AI 分析未完成；保留 {len(picks)} 只已验证候选') if picks else \
                ('failed', 'AI 分析或数据未完成，本轮未产生新推荐')
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
        by_id = {e.get('call_id'): e for e in tool_log}
        sources = [{'call_id': c['call_id'], 'tool': by_id[c['call_id']].get('tool'), 'url': c.get('url'),
                    'summary': c.get('summary')} for c in pick['citations']]
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
