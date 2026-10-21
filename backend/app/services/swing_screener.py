"""Transparent, causal 2–4 week screening; adapted VCP/CANSLIM factors, not win probabilities."""
import math
import pandas as pd

STOCK_UNIVERSE = {
    'NVDABUSDT':'NVDA', 'TSLABUSDT':'TSLA', 'AAPLBUSDT':'AAPL', 'MSFTBUSDT':'MSFT',
    'AMZNBUSDT':'AMZN', 'GOOGBUSDT':'GOOG', 'METABUSDT':'META', 'COINBUSDT':'COIN',
}
HORIZON = '2～4 周'


def _frame(rows):
    df = pd.DataFrame(rows)
    if not {'date','open','high','low','close','volume'} <= set(df.columns):
        raise ValueError('日线字段不足')
    df = df.drop_duplicates('date').sort_values('date').set_index('date')
    for key in ('open','high','low','close','volume'):
        df[key] = pd.to_numeric(df[key], errors='raise')
        if any(not math.isfinite(float(v)) or (v <= 0 if key != 'volume' else v < 0) for v in df[key]):
            raise ValueError('日线存在无效价格')
    if (df['high'] < df['low']).any() or (df['close'] > df['high']).any() or (df['close'] < df['low']).any():
        raise ValueError('OHLC 不一致')
    return df


def score_swing(history, benchmark, financials=None):
    reject = lambda why: {'eligible':False,'score':0,'signals':[],'metrics':{},'risks':[why]}
    try:
        df, market = _frame(history), _frame(benchmark)
        if len(df) < 252 or len(market) < 60:
            return reject('至少需要 252 根个股日线和 60 根基准日线')
        # Align common dates; different session windows must not be compared.
        common = df.index.intersection(market.index).sort_values()
        if len(common) < 60 or common[-1] != df.index[-1]:
            return reject('个股与基准交易日不一致')
        c, h, l, v = (df[key] for key in ('close','high','low','volume'))
        price = float(c.iloc[-1])
        ma20, ma50, ma150, ma200 = (float(c.iloc[-n:].mean()) for n in (20,50,150,200))
        ma50_past = float(c.iloc[-70:-20].mean())
        ma200_past = float(c.iloc[-220:-20].mean())
        if not (price > ma50 > ma150 and price > ma200 and ma50 > ma50_past):
            return reject('中期趋势未满足多头筛选条件')
        rel = float((c.loc[common].iloc[-1]/c.loc[common].iloc[-21]-1) -
                    (market.loc[common,'close'].iloc[-1]/market.loc[common,'close'].iloc[-21]-1))
        true_range = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        atr = float(true_range.iloc[-14:].mean())
        pivot = float(h.iloc[-21:-1].max())
        previous_range = float(((h-l)/c).iloc[-30:-10].mean())
        recent_range = float(((h-l)/c).iloc[-10:].mean())
        volume_ratio = float(v.iloc[-10:].mean()/v.iloc[-50:-10].mean()) if v.iloc[-50:-10].mean() > 0 else 1
        if atr <= 0 or price > ma20 + 3*atr or price > pivot + 2*atr:
            return reject('价格过度延伸，不追高')
        entry_low = round(max(ma20, price-atr*.5), 4)
        entry_high = round(max(price, pivot+atr*.1), 4)
        stop = round(float(l.iloc[-10:].min())-atr*.2, 4)
        risk = entry_high-stop
        if not 0 < stop < entry_low <= entry_high or not .003 <= risk/entry_high <= .12:
            return reject('观察区间与风险距离不合理')
        score, signals = 30, ['价格高于 MA50/150/200，MA50 向上']
        if ma200 > ma200_past:
            score += 10
            signals.append('MA200 向上')
        if rel > 0:
            score += 15
            signals.append('近 20 个交易日强于 SPY')
        if recent_range < previous_range*.8:
            score += 15
            signals.append('近期振幅相较前段收缩（VCP 因子，非完整形态认证）')
        if volume_ratio < .85:
            score += 10
            signals.append('收缩阶段成交量下降')
        if price >= float(h.iloc[-252:].max())*.85:
            score += 5
            signals.append('接近一年高位区域')
        eps_growth = revenue_growth = None
        risks = ['技术评分不是上涨概率；形态可能失效']
        if financials:
            statements = sorted(financials, key=lambda x:str(x.get('date','')), reverse=True)
            if len(statements) >= 5:
                latest, prior = statements[0], statements[4]
                for key in ('eps','revenue'):
                    current, old = float(latest.get(key) or 0), float(prior.get(key) or 0)
                    if not math.isfinite(current) or not math.isfinite(old):
                        raise ValueError('财务字段无效')
                    growth = current/old-1 if old > 0 else None
                    if key == 'eps': eps_growth = growth
                    else: revenue_growth = growth
                if eps_growth is not None and eps_growth >= .25:
                    score += 10
                    signals.append('季度 EPS 同比增长至少 25%')
                if revenue_growth is not None and revenue_growth >= .15:
                    score += 5
                    signals.append('季度营收同比增长至少 15%')
                if eps_growth is None or eps_growth < 0:
                    risks.append('EPS 同比无法可靠计算或出现下滑')
            else:
                risks.append('季度历史不足，CANSLIM 盈利同比因子未计分')
        else:
            risks.append('基本面尚待复核，当前为技术面初筛分')
        market_close = market['close']
        if market_close.iloc[-1] < market_close.iloc[-50:].mean():
            risks.append('SPY 位于 MA50 下方，市场环境偏弱')
        return {'eligible':True,'score':min(score,100),'signals':signals,'risks':risks,
            'metrics':{'close':price,'ma20':ma20,'ma50':ma50,'ma150':ma150,'ma200':ma200,
                'atr14':atr,'pivot':pivot,'relative_strength_20d':rel,
                'contraction_ratio':recent_range/previous_range if previous_range>0 else None,
                'volume_ratio':volume_ratio,'eps_growth_yoy':eps_growth,'revenue_growth_yoy':revenue_growth},
            'entry_range':[entry_low,entry_high], 'stop_loss_price':stop,
            'target_price':round(entry_high+2*risk,4),
            'level_method':'近 20 日高点/MA20 观察区间；近 10 日低点减 0.2ATR 止损；2R 观察目标，非价格预测'}
    except (KeyError,ValueError,TypeError,ZeroDivisionError,IndexError):
        return reject('历史数据或财务字段不足/无效，已跳过')
