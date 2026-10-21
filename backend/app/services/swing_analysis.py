"""Evidence-driven daily agent workflow for the original Binance-linked equity pool."""
import asyncio
from datetime import date, datetime, timezone
import time

from pydantic import ValidationError

from backend.app.services.analysis_errors import AnalysisUnavailable, AnalysisBusy, SwingJudgment
from backend.app.services.analysis_skills import skill_metadata
from backend.app.services.analysis_store import AnalysisStore, timestamp
from backend.app.services.swing_screener import STOCK_UNIVERSE, HORIZON, score_swing, _frame
from backend.app.services.us_market_data import USMarketData, MarketDataError


def split_message(text, max_bytes=1800):
    chunks, current, size = [], [], 0
    for char in text:
        width = len(char.encode('utf-8'))
        if size+width > max_bytes:
            chunks.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        chunks.append(''.join(current))
    return chunks


class SwingAnalysisService:
    def __init__(self, market, agent, notifier, db_path=None):
        self.market, self.agent, self.notifier = market, agent, notifier
        self.store = AnalysisStore(db_path)
        self._lock = asyncio.Lock()
        self._push_lock = asyncio.Lock()
        self._position_locks = {}
        self._position_cache = {}

    @property
    def is_running(self):
        return self._lock.locked()

    def require_config(self):
        if not self.market.api_key:
            raise AnalysisUnavailable('请在配置中心填写 FMP API Key，并确认日线、财务、新闻和财报日历权限')
        if not self.agent.api_key:
            raise AnalysisUnavailable('请在配置中心填写分析模型 API Key；不会用固定涨幅冒充 AI 推荐')

    async def _evidence(self, symbol, history, benchmark, research):
        underlying = STOCK_UNIVERSE[symbol]
        scored = score_swing(history, benchmark, research['financials'])
        sources = USMarketData(api_key='').sources(underlying)
        sources[0]['as_of'] = history[-1]['date']
        return {'symbol':symbol,'underlying_symbol':underlying,'horizon':HORIZON,
            'price_basis':'US_EQUITY_USD','as_of':history[-1]['date'],
            'skills':skill_metadata(), 'scored':scored,'recent_sessions':history[-20:],
            'financials':research['financials'],'news':research['news'],'earnings':research['earnings'],
            'sources':sources+research['sources']}

    async def _report(self, evidence):
        try:
            judgment = SwingJudgment.model_validate(await self.agent.analyze_swing(evidence)).model_dump()
        except (ValidationError,ValueError,TypeError):
            raise AnalysisUnavailable('分析模型输出结构无效，已拒绝作为推荐') from None
        ids={source['id'] for source in evidence['sources']}
        if not set(judgment['source_ids']) <= ids:
            raise AnalysisUnavailable('分析模型引用了未提供的来源，已拒绝作为推荐')
        scored = evidence['scored']
        return {'symbol':evidence['symbol'],'underlying_symbol':evidence['underlying_symbol'],
            'selected':judgment['selected'],'reason':judgment['reason'],'invalidation':judgment['invalidation'],
            'risks':list(dict.fromkeys(scored['risks']+judgment['risks'])),
            'score':scored['score'],'signals':scored['signals'],'metrics':scored['metrics'],
            'entry_range':scored.get('entry_range'), 'target_price':scored.get('target_price'),
            'stop_loss_price':scored.get('stop_loss_price'),'level_method':scored.get('level_method'),
            'horizon':HORIZON,'price_basis':'US_EQUITY_USD','as_of':evidence['as_of'],
            'sources':evidence['sources'],'source_ids':judgment['source_ids'],
            'catalysts':evidence['earnings'],'analysis_mode':'llm','skills':skill_metadata()}

    async def run(self, top_n=3, min_score=60, earnings_blackout_days=3,
                  push_notifications=False, run_key=None):
        self.require_config()
        if not 3 <= top_n <= 5 or not 40 <= min_score <= 100 or not 0 <= earnings_blackout_days <= 14:
            raise ValueError('波段筛选参数超出允许范围')
        if self._lock.locked():
            raise AnalysisBusy('已有分析正在运行，请稍后查看结果')
        async with self._lock:
            run_id, claimed = await self.store.begin_run(run_key,push_notifications)
            if not claimed:
                row = await self.store.get_run(run_id)
                return {'run_id':run_id,'count':row['count'],'picks':await self.store.list_picks(run_id),
                    'status':row['status'],'message':row['message']}
            errors, diagnostics, picks, candidates = [], [], [], []
            try:
                # SPY is context only and can never become a recommendation.
                benchmark = await self.market.get_history('SPY')
                _frame(benchmark)
                for symbol, underlying in STOCK_UNIVERSE.items():
                    try:
                        history = await self.market.get_history(underlying)
                        _frame(history)
                        if len(history) < 252:
                            raise MarketDataError('至少需要 252 根完整日线')
                        preliminary = score_swing(history,benchmark)
                        if not preliminary['eligible']:
                            diagnostics.append({'symbol':symbol,'decision':'filtered','reason':preliminary['risks'][0]})
                            continue
                        research = await self.market.get_research(underlying)
                        if not research['financials']:
                            raise MarketDataError('财务证据缺失')
                        today = datetime.now(timezone.utc).date()
                        near_earnings = [event for event in research['earnings']
                            if 0 <= (date.fromisoformat(event['date'])-today).days <= earnings_blackout_days]
                        if near_earnings and earnings_blackout_days:
                            diagnostics.append({'symbol':symbol,'decision':'filtered','reason':'处于财报风险禁入窗口'})
                            continue
                        evidence = await self._evidence(symbol,history,benchmark,research)
                        if evidence['scored']['eligible'] and evidence['scored']['score'] >= min_score:
                            candidates.append(evidence)
                        else:
                            diagnostics.append({'symbol':symbol,'decision':'filtered','reason':'未达到波段筛选阈值'})
                    except (MarketDataError,ValueError,KeyError,TypeError) as exc:
                        # Do not persist provider text except our own safe adapter errors.
                        reason = str(exc) if isinstance(exc,MarketDataError) else '数据字段校验未通过'
                        errors.append({'symbol':symbol,'stage':'data','message':reason})
                candidates.sort(key=lambda item:item['scored']['score'],reverse=True)
                for evidence in candidates:
                    if len(picks) >= top_n:
                        break
                    try:
                        report = await self._report(evidence)
                        if report['selected']:
                            picks.append(report)
                        diagnostics.append({'symbol':evidence['symbol'],
                            'decision':'selected' if report['selected'] else 'rejected','reason':report['reason']})
                    except AnalysisUnavailable as exc:
                        errors.append({'symbol':evidence['symbol'],'stage':'model','message':str(exc)})
                status = ('partial' if picks else 'failed') if errors else ('completed' if picks else 'no_candidates')
                message = f'完成 {HORIZON}波段分析，筛选出 {len(picks)} 只条件性候选' if picks else '本次没有符合条件的候选，不凑数'
                if errors:
                    message = f'部分数据或模型不可用；保留 {len(picks)} 只已验证候选' if picks else '数据或模型未完成复核，未产生新推荐，保留上一份可用结果'
                await self.store.finish_run(run_id,status,picks,errors,diagnostics,message)
            except asyncio.CancelledError:
                await self.store.finish_run(run_id,'failed',[],[],[],'分析被中断，可稍后重新运行')
                raise
            except Exception as exc:
                message = str(exc) if isinstance(exc,(MarketDataError,AnalysisUnavailable)) else '分析执行异常，请检查数据与模型服务'
                await self.store.finish_run(run_id,'failed',[],[{'stage':'run','message':message}],[],message)
                status='failed'
            if push_notifications:
                await self.push_run(run_id)
            row=await self.store.get_run(run_id)
            return {'run_id':run_id,'count':row['count'],'picks':await self.store.list_picks(run_id),
                'status':row['status'],'message':row['message']}

    async def analyze_position(self, symbol):
        self.require_config()
        if symbol not in STOCK_UNIVERSE:
            raise AnalysisUnavailable('该持仓不在原币安美股标的池映射内，无法混用其他资产分析')
        lock=self._position_locks.setdefault(symbol,asyncio.Lock())
        async with lock:
            cached=self._position_cache.get(symbol)
            if cached and time.monotonic()-cached[0] < 600:
                return cached[1]
            try:
                history=await self.market.get_history(STOCK_UNIVERSE[symbol])
                benchmark=await self.market.get_history('SPY')
                research=await self.market.get_research(STOCK_UNIVERSE[symbol])
                evidence=await self._evidence(symbol,history,benchmark,research)
                # Position risk analysis may be bearish; it is not a new-buy selection.
                evidence['purpose']='持仓风险复核；允许看空和不建议加仓，不能改动实际持仓止盈止损'
                report=await self._report(evidence)
            except MarketDataError as exc:
                raise AnalysisUnavailable(str(exc)) from None
            self._position_cache[symbol]=(time.monotonic(),report)
            return report

    def _messages(self, run, picks):
        text=f'【每日美股波段精选 · {HORIZON}】\n分析批次 #{run["id"]}\n{run["message"]}\n'
        for pick in picks:
            low,high=pick['entry_range']
            text+=(f'\n{pick["underlying_symbol"]} / {pick["symbol"]} | 规则分 {pick["score"]}/100\n'
                f'底层美股 USD（不是币安成交价），基准日 {pick["as_of"]}\n'
                f'观察区间 {low:.2f}–{high:.2f}；2R观察目标 {pick["target_price"]:.2f}；失效参考 {pick["stop_loss_price"]:.2f}\n'
                f'逻辑：{pick["reason"][:240]}\n失效：{pick["invalidation"][:120]}\n'
                f'风险：{"；".join(pick["risks"][:3])[:200]}\n')
            for source in pick['sources'][:2]:
                text+=f'来源：{source["title"][:80]} {source["url"]}\n'
        if run['errors']:
            text+='\n部分数据或模型未通过复核，详见控制台运行记录。\n'
        text+='\n条件性观察名单，不保证上涨，不执行交易；价格口径不同，禁止直接复制为币安委托价。'
        return split_message(text)

    async def push_run(self, run_id, force=True):
        async with self._push_lock:
            run=await self.store.get_run(run_id)
            if not run:
                raise LookupError('分析记录不存在')
            if run['status']=='running':
                raise AnalysisBusy('分析尚未完成')
            channels=[name for name, url in [('feishu',self.notifier.feishu_webhook),('wecom',self.notifier.wecom_webhook)] if url]
            picks=await self.store.list_picks(run_id,include_hidden=True)
            await self.store.queue_deliveries(run_id,channels,self._messages(run,picks))
            rows=await self.store.rows('SELECT * FROM analysis_deliveries WHERE run_id=? ORDER BY channel,part',(run_id,))
            for delivery in rows:
                if delivery['channel'] not in channels or not await self.store.claim_delivery(delivery['id'],force):
                    continue
                try:
                    success=bool(await self.notifier.send_channel_text(delivery['channel'],delivery['body']))
                except Exception:
                    success=False
                await self.store.delivery_result(delivery,success)
            status=await self.store.update_push_status(run_id)
            return {'success':status=='success','status':status,
                'message':'所有渠道推送成功' if status=='success' else '推送未全部成功，已保留失败状态，可配置渠道后重试'}

    async def retry_pushes(self):
        rows=await self.store.rows("SELECT id FROM analysis_runs WHERE push_requested=1 AND push_status IN ('pending','failed','partial') ORDER BY id DESC LIMIT 20")
        for row in rows:
            await self.push_run(row['id'],force=False)

    async def review(self):
        if not self.market.api_key:
            raise AnalysisUnavailable('复盘需要配置 FMP 数据源')
        picks=await self.store.list_picks(include_hidden=True,all_runs=True)
        pending=[p for p in picks if p.get('price_basis')=='US_EQUITY_USD']
        if not pending:
            return {'count':0,'message':'还没有可复盘的波段推荐'}
        benchmark=await self.market.get_history('SPY')
        bench={b['date']:b['close'] for b in benchmark}
        cache={}
        count=0
        errors=[]
        for pick in pending:
            completed=await self.store.rows('SELECT horizon_sessions FROM analysis_reviews WHERE pick_id=?',(pick['id'],))
            done={row['horizon_sessions'] for row in completed}
            if done == {10,20}:
                continue
            symbol=pick['underlying_symbol']
            try:
                if symbol not in cache:
                    cache[symbol]=await self.market.get_history(symbol)
                history=cache[symbol]
                bases={bar['date']:bar['close'] for bar in history}
                subsequent=[bar for bar in history if bar['date']>pick['as_of']]
                for sessions in (10,20):
                    if sessions in done or len(subsequent)<sessions:
                        continue
                    bar=subsequent[sessions-1]
                    if pick['as_of'] not in bases or pick['as_of'] not in bench or bar['date'] not in bench:
                        continue
                    change=(bar['close']/bases[pick['as_of']]-1)*100
                    market_change=(bench[bar['date']]/bench[pick['as_of']]-1)*100
                    count+=await self.store.execute('''INSERT OR IGNORE INTO analysis_reviews
                        (pick_id,horizon_sessions,as_of,return_pct,benchmark_return_pct,excess_return_pct) VALUES (?,?,?,?,?,?)''',
                        (pick['id'],sessions,bar['date'],round(change,4),round(market_change,4),round(change-market_change,4)))
            except MarketDataError:
                errors.append(symbol)
        return {'count':count,'errors':errors,'message':'基于实际 10/20 个后续交易日收盘价的观察收益；不是含成本交易回测'}
