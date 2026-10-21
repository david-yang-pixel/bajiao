"""Conservative acquisition-cost ledger, preserving actual payment currencies.

Only an explicit successful token redemption can move cost between instruments.
No stablecoin/USD peg or token/share conversion ratio is assumed here.
"""
from decimal import Decimal, InvalidOperation
import math
import re


class EquityCostUnavailable(ValueError):
    pass


def decimal(value, *, zero=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (not zero and result == 0):
            raise EquityCostUnavailable('Invalid ledger amount')
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise EquityCostUnavailable('Invalid ledger amount') from None


class Inventory:
    def __init__(self):
        self.quantity = Decimal(0)
        self.costs = {}

    def buy(self, quantity, costs):
        if quantity <= 0 or not costs or any(k not in {'USD', 'USDT', 'USDC'} for k in costs):
            raise EquityCostUnavailable('Unsupported acquisition currency')
        self.quantity += quantity
        for currency, cost in costs.items():
            self.costs[currency] = self.costs.get(currency, Decimal(0)) + cost

    def sell(self, quantity):
        if self.quantity <= 0 or quantity > self.quantity + Decimal('0.000000001'):
            raise EquityCostUnavailable('Missing inventory acquisition')
        sold = {k: v * min(quantity, self.quantity) / self.quantity for k, v in self.costs.items()}
        self.quantity = max(Decimal(0), self.quantity - quantity)
        for currency, cost in sold.items():
            self.costs[currency] -= cost
        if self.quantity == 0:
            self.costs = {}
        return sold


def reconstruct_equity_cost(*, symbol, quantity, token_quantity, conversions, spot, flows, trades):
    """Return one-currency per-share cost only after inventory reconciliation.

    The caller proves source completeness/window bounds. This pure function proves
    identity, event order, cost continuity, redemption ratios and remaining shares.
    Third-asset fees are not converted into the acquisition currency.
    """
    if not re.fullmatch(r'EQ_[A-Z][A-Z0-9.\-]{0,19}', symbol):
        raise EquityCostUnavailable('Invalid equity identity')
    underlying = symbol[3:]
    conversions = [t for t in conversions if t.get('underlyingAsset') == underlying and t.get('status') == 'S']
    tokens = {t['tokenizedAsset'] for t in conversions}
    if len(tokens) != 1:
        raise EquityCostUnavailable('No unambiguous token redemption mapping')
    token = next(iter(tokens))
    if not re.fullmatch(r'[A-Z][A-Z0-9]{0,24}', token):
        raise EquityCostUnavailable('Invalid token identity')
    events, ids, time_operations = [], set(), {}

    def add(kind, identifier, timestamp, operation, row):
        identity = (kind, str(identifier))
        if identity in ids:
            raise EquityCostUnavailable('Duplicate ledger event')
        ids.add(identity)
        timestamp = int(timestamp)
        if timestamp < 0:
            raise EquityCostUnavailable('Invalid event timestamp')
        operations = time_operations.setdefault(timestamp, set())
        operations.add(operation)
        if len(operations) > 1:
            raise EquityCostUnavailable('Ambiguous same-time inventory order')
        events.append((timestamp, kind, row))

    for row in spot:
        if row.get('symbol') != token + 'USDT' or not isinstance(row.get('isBuyer'), bool):
            raise EquityCostUnavailable('Spot identity mismatch')
        add('spot', row['id'], row['time'], 'token_buy' if row['isBuyer'] else 'token_sell', row)
    for row in flows:
        if token not in (row.get('fromAsset'), row.get('toAsset')) or row.get('orderStatus') != 'SUCCESS':
            continue
        add('convert', row['orderId'], row['createTime'], 'token_buy' if row['toAsset'] == token else 'token_sell', row)
    for row in conversions:
        if row['convertType'] != 'REDEEM':
            raise EquityCostUnavailable('Unsupported stock-to-token cycle')
        add('redemption', row['issuerRequestId'], row['updatedAt'], 'redemption', row)
    for row in trades:
        if row.get('symbol') != underlying or row.get('side') not in {'BUY', 'SELL'}:
            raise EquityCostUnavailable('Native trade identity mismatch')
        add('native', row['executionId'], row['executionAt'], 'native_' + row['side'], row)

    token_pool, native_pool = Inventory(), Inventory()
    for _, kind, row in sorted(events, key=lambda event: event[0]):
        if kind == 'spot':
            qty = decimal(row['qty'])
            fee = decimal(row.get('commission', 0), zero=True)
            base_fee = fee if row.get('commissionAsset') == token else Decimal(0)
            if row['isBuyer']:
                quote_fee = fee if row.get('commissionAsset') == 'USDT' else Decimal(0)
                token_pool.buy(qty - base_fee, {'USDT': decimal(row['quoteQty']) + quote_fee})
            else:
                token_pool.sell(qty + base_fee)
        elif kind == 'convert':
            if row['toAsset'] == token:
                token_pool.buy(decimal(row['toAmount']), {row['fromAsset']: decimal(row['fromAmount'])})
            else:
                token_pool.sell(decimal(row['fromAmount']))
        elif kind == 'redemption':
            amount = decimal(row['tokenizedAssetAmount'])
            shares = decimal(row['underlyingAssetAmount'])
            transferred = token_pool.sell(amount)
            native_pool.buy(shares, transferred)
        else:
            qty = decimal(row['qty'])
            if row['side'] == 'BUY':
                native_pool.buy(qty, {'USD': qty * decimal(row['price'])})
            else:
                native_pool.sell(qty)
    if not math.isclose(float(token_pool.quantity), float(decimal(token_quantity, zero=True)),
                        rel_tol=1e-7, abs_tol=1e-9):
        raise EquityCostUnavailable('Unexplained token opening/remaining inventory')
    expected = decimal(quantity)
    if not math.isclose(float(native_pool.quantity), float(expected), rel_tol=1e-7, abs_tol=1e-9):
        raise EquityCostUnavailable('Remaining stock balance mismatch')
    costs = {k: v for k, v in native_pool.costs.items() if v > 0}
    if len(costs) != 1 or native_pool.quantity <= 0:
        raise EquityCostUnavailable('Cost requires unprovided historical FX conversion')
    currency, total = next(iter(costs.items()))
    price = float(total / native_pool.quantity)
    if not math.isfinite(price) or price <= 0:
        raise EquityCostUnavailable('Invalid reconstructed cost')
    return {'price': price, 'currency': currency}
