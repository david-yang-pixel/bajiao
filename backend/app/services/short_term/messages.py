"""Push message text for the short-term pick workflow."""
from datetime import date

FOOTER = '研究价格以正股 USD 为准，USDT 为按比例换算的参考；不保证上涨，不执行交易。'


def split_message(text, max_bytes=1800):
    chunks, current, size = [], [], 0
    for char in text:
        width = len(char.encode('utf-8'))
        if size + width > max_bytes:
            chunks.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        chunks.append(''.join(current))
    return chunks


def _pick_block(p: dict) -> str:
    sym, under = p['symbol'], p.get('underlying_symbol') or p['symbol']
    until = date.fromisoformat(p['expires_after']).strftime('%m-%d')
    lines = [f'{under} / {sym}  短线·1周·有效至 {until}',
             f"正股 USD 现价 {p['current_price_usd']:.2f}  目标 {p['target_price_usd']:.2f} ({p['target_pct']:+.1f}%)  "
             f"止损 {p['stop_price_usd']:.2f} ({p['stop_pct']:+.1f}%)  盈亏比 {p['reward_risk']:.1f}",
             f"代币 {sym} 现价 {p['token_price_usdt']:.2f} → 目标约 {p['token_target_usdt']:.2f} / "
             f"止损约 {p['token_stop_usdt']:.2f}（按比例换算）",
             f"依据：{p['reason'][:240]}", f"失效：{p['invalidation'][:120]}",
             f"风险：{'；'.join(p['risks'][:3])[:200]}"]
    lines += [f"来源：{s['url']}" for s in [s for s in p.get('sources') or [] if s.get('url')][:2]]
    return '\n'.join(lines)


def _event_line(e: dict) -> str:
    under, sym, price = e.get('underlying_symbol') or e.get('symbol'), e.get('symbol'), e.get('price_usd')
    return {'target_hit': lambda: f'✅ {under} 已到目标 {price:.2f}',
            'stopped': lambda: f'⛔ {under} 触发止损 {price:.2f}',
            'expired': lambda: f'⌛ {under} 到期未达目标',
            'halted': lambda: f'⚠️ {sym} 现货交易对暂停交易'}[e['event']]()


def build_run_message(run: dict, new_picks: list[dict], events: list[dict], tracking_count: int) -> list[str]:
    if not new_picks and not events:
        head = f'【短线精选 · 1周】本轮无新增，{tracking_count} 只跟踪中'
    else:
        head = f'【短线精选 · 1周】本轮新增 {len(new_picks)} 只，{tracking_count} 只跟踪中'
    parts = [head]
    parts += [_pick_block(p) for p in new_picks]
    if events:
        parts.append('\n'.join(_event_line(e) for e in events))
    parts.append(FOOTER)
    return split_message('\n\n'.join(parts), 1800)
