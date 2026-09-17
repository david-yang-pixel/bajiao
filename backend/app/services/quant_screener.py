import logging
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

def calc_sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()

def calc_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=length, min_periods=length).mean()
    avg_loss = loss.rolling(window=length, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calc_bbands(series: pd.Series, length: int = 20, std: float = 2.0):
    mid = series.rolling(window=length, min_periods=length).mean()
    rolling_std = series.rolling(window=length, min_periods=length).std()
    upper = mid + (rolling_std * std)
    lower = mid - (rolling_std * std)
    return lower, mid, upper

class QuantScreener:
    """
    Computes technical indicators (MA5, MA10, MA20, RSI, Bollinger Bands, Volume Surge)
    and scores bStock tickers for potential breakout / upside signals.
    """

    def calculate_indicators(self, klines: List[Dict[str, Any]]) -> pd.DataFrame:
        """
        Convert list of klines to pandas DataFrame and compute technical indicators:
        - MA5, MA10, MA20
        - RSI(14)
        - Bollinger Bands (20, 2)
        - Volume 5-period average
        """
        if not klines:
            return pd.DataFrame()

        df = pd.DataFrame(klines)
        # Ensure numeric types
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        if len(df) < 5:
            return df

        # Moving averages
        df["ma5"] = calc_sma(df["close"], length=5)
        df["ma10"] = calc_sma(df["close"], length=10)
        df["ma20"] = calc_sma(df["close"], length=20)

        # RSI 14
        if len(df) >= 14:
            df["rsi"] = calc_rsi(df["close"], length=14)
        else:
            df["rsi"] = None

        # Bollinger Bands
        if len(df) >= 20:
            lower, mid, upper = calc_bbands(df["close"], length=20, std=2.0)
            df["bb_lower"] = lower
            df["bb_mid"] = mid
            df["bb_upper"] = upper
        else:
            df["bb_lower"] = None
            df["bb_mid"] = None
            df["bb_upper"] = None

        # 5-period average volume
        df["vol_ma5"] = calc_sma(df["volume"], length=5)

        return df

    def score_stock(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Calculates a technical score (0-100) based on latest indicators:
        - Bullish MA alignment (Close > MA5 > MA10 > MA20): +30 pts
        - Healthy RSI (40 <= RSI <= 65, upward room): +20 pts
        - Volume surge (latest vol > 1.5 * vol_ma5): +25 pts
        - Bollinger upper band breakout / squeeze expansion: +25 pts
        """
        if df.empty:
            return {"score": 0, "signals": [], "metrics": {}}

        latest = df.iloc[-1]
        score = 0
        signals = []

        close = latest.get("close", 0.0)
        ma5 = latest.get("ma5")
        ma10 = latest.get("ma10")
        ma20 = latest.get("ma20")
        rsi = latest.get("rsi")
        vol = latest.get("volume", 0.0)
        vol_ma5 = latest.get("vol_ma5")
        bb_upper = latest.get("bb_upper")
        bb_mid = latest.get("bb_mid")
        bb_lower = latest.get("bb_lower")

        # 1. Trend: MA Bullish Alignment (+30 pts)
        if pd.notnull(ma5) and pd.notnull(ma10) and pd.notnull(ma20):
            if close > ma5 > ma10 > ma20:
                score += 30
                signals.append("MA多头排列(Close>MA5>MA10>MA20)")
            elif close > ma5 and ma5 > ma10:
                score += 15
                signals.append("短期均线金叉多头(Close>MA5>MA10)")

        # 2. Momentum: RSI Health (+20 pts)
        if pd.notnull(rsi):
            if 40.0 <= rsi <= 65.0:
                score += 20
                signals.append(f"RSI位于蓄势健康区间({rsi:.1f})")
            elif 65.0 < rsi <= 75.0:
                score += 10
                signals.append(f"RSI强势多头({rsi:.1f})")

        # 3. Volume Surge (+25 pts)
        if pd.notnull(vol_ma5) and vol_ma5 > 0:
            vol_ratio = vol / vol_ma5
            if vol_ratio >= 1.5:
                score += 25
                signals.append(f"放量异动突破(成交量超5日均量{vol_ratio:.1f}倍)")
            elif vol_ratio >= 1.2:
                score += 15
                signals.append(f"温和放量(成交量超5日均量{vol_ratio:.1f}倍)")
        else:
            vol_ratio = 1.0

        # 4. Volatility: Bollinger Bands (+25 pts)
        if pd.notnull(bb_upper) and pd.notnull(bb_lower):
            if close >= bb_upper:
                score += 25
                signals.append("触碰/突破布林带上轨强势上涨")
            elif close > bb_mid:
                score += 15
                signals.append("运行于布林带中轨上方偏强震荡")

        metrics = {
            "close": float(close) if pd.notnull(close) else 0.0,
            "ma5": float(ma5) if pd.notnull(ma5) else None,
            "ma10": float(ma10) if pd.notnull(ma10) else None,
            "ma20": float(ma20) if pd.notnull(ma20) else None,
            "rsi": float(rsi) if pd.notnull(rsi) else None,
            "volume": float(vol) if pd.notnull(vol) else 0.0,
            "vol_ma5": float(vol_ma5) if pd.notnull(vol_ma5) else None,
            "vol_ratio": float(vol_ratio) if pd.notnull(vol_ratio) else 1.0,
            "bb_upper": float(bb_upper) if pd.notnull(bb_upper) else None,
            "bb_lower": float(bb_lower) if pd.notnull(bb_lower) else None,
        }

        return {
            "score": min(score, 100),
            "signals": signals,
            "metrics": metrics
        }

    async def screen_candidates(self, client: Any, min_score: int = 40) -> List[Dict[str, Any]]:
        """
        Screen all supported bStocks symbols using technical indicators and return candidates
        with score >= min_score, ordered descending by score.
        """
        symbols = client.get_supported_bstocks()
        if hasattr(symbols, "__await__"):
            symbols = await symbols
        candidates = []

        for sym in symbols:
            try:
                klines = await client.get_klines(sym, interval="1d", limit=60)
                if not klines or len(klines) < 5:
                    continue
                df = self.calculate_indicators(klines)
                scored = self.score_stock(df)
                if scored["score"] >= min_score:
                    candidates.append({
                        "symbol": sym,
                        "score": scored["score"],
                        "signals": scored["signals"],
                        "metrics": scored["metrics"]
                    })
            except Exception as e:
                logger.warning(f"Error screening symbol {sym}: {e}")

        # Sort descending by score
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates
