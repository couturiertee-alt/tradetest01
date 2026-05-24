"""
Translates a StrategyBlueprint JSON into executable Python code.
Produces two flavours: backtrader (backtesting) and ccxt (paper/live).
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

from config import STRATEGIES_DIR
from parser.schema import Indicator, StrategyBlueprint

# ---------------------------------------------------------------------------
# Indicator → Backtrader mapping
# ---------------------------------------------------------------------------

_BT_INDICATOR_MAP = {
    "EMA":        "bt.indicators.EMA",
    "SMA":        "bt.indicators.SMA",
    "WMA":        "bt.indicators.WeightedMovingAverage",
    "RSI":        "bt.indicators.RSI",
    "MACD":       "bt.indicators.MACD",
    "BB":         "bt.indicators.BollingerBands",
    "BBANDS":     "bt.indicators.BollingerBands",
    "ATR":        "bt.indicators.ATR",
    "STOCH":      "bt.indicators.Stochastic",
    "STOCHASTIC": "bt.indicators.Stochastic",
    "ADX":        "bt.indicators.DirectionalMovement",
    "VWAP":       "bt.indicators.VWAP",
    "CCI":        "bt.indicators.CCI",
}

_PANDAS_INDICATOR_MAP = {
    "EMA":    lambda df, p: f"df['close'].ewm(span={p}, adjust=False).mean()",
    "SMA":    lambda df, p: f"df['close'].rolling({p}).mean()",
    "RSI":    lambda df, p: f"_rsi(df['close'], {p})",
    "ATR":    lambda df, p: f"_atr(df, {p})",
    "BB":     lambda df, p: f"_bollinger(df['close'], {p})",
}


def _var_name(ind: Indicator) -> str:
    """Produce a safe Python variable name for an indicator instance."""
    period = ind.params.get("period", ind.params.get("fast_period", ""))
    suffix = f"_{period}" if period else ""
    return f"{ind.name.lower()}{suffix}"


def _bt_indicator_line(ind: Indicator) -> str:
    bt_cls = _BT_INDICATOR_MAP.get(ind.name.upper(), f"bt.indicators.{ind.name}")
    var = _var_name(ind)

    # Build kwargs string
    needs_data_arg = ind.name.upper() in {"ATR", "STOCH", "STOCHASTIC", "ADX"}
    data_arg = "self.data" if needs_data_arg else "self.data.close"

    period = ind.params.get("period")
    kwargs = ""
    if period:
        kwargs = f", period={period}"
    elif ind.params:
        kwargs = ", " + ", ".join(f"{k}={v}" for k, v in ind.params.items())

    return f"        self.{var} = {bt_cls}({data_arg}{kwargs})"


def _translate_condition_bt(cond: str, indicators: list[Indicator]) -> str:
    """Best-effort translation of a natural language condition to Backtrader code."""
    c = cond.lower()
    # Strip parenthetical params for pattern matching (RSI(14) → RSI)
    c_clean = re.sub(r'\(\d+\)', '', c)

    # Crossover patterns
    cross_above = re.search(r"(\w+)\s+crosses?\s+above\s+(\w+)", c_clean)
    cross_below = re.search(r"(\w+)\s+crosses?\s+below\s+(\w+)", c_clean)

    if cross_above:
        a, b = cross_above.group(1), cross_above.group(2)
        a_ref = _resolve_ref(a, indicators)
        b_ref = _resolve_ref(b, indicators)
        # Manual two-bar crossover check — avoids creating a CrossOver indicator in next()
        return f"({a_ref}[0] > {b_ref}[0] and {a_ref}[-1] <= {b_ref}[-1])"

    if cross_below:
        a, b = cross_below.group(1), cross_below.group(2)
        a_ref = _resolve_ref(a, indicators)
        b_ref = _resolve_ref(b, indicators)
        return f"({a_ref}[0] < {b_ref}[0] and {a_ref}[-1] >= {b_ref}[-1])"

    # Simple numeric comparisons  e.g. "RSI(14) < 30", "rsi < 30"
    num_cmp = re.search(r"(\w+)(?:\(\d+\))?\s*([<>]=?)\s*(\d+\.?\d*)", c)
    if num_cmp:
        ref_raw, op, val = num_cmp.group(1), num_cmp.group(2), num_cmp.group(3)
        ref = _resolve_ref(ref_raw, indicators)
        return f"{ref}[0] {op} {val}"

    # Price vs indicator  e.g. "close > EMA_20"
    price_vs = re.search(r"(close|open|high|low|price)\s*([<>]=?)\s*(\w+)", c_clean)
    if price_vs:
        side, op, ind_raw = price_vs.group(1), price_vs.group(2), price_vs.group(3)
        price_ref = "self.data.close[0]" if side in ("close", "price") else f"self.data.{side}[0]"
        ind_ref = _resolve_ref(ind_raw, indicators)
        return f"{price_ref} {op} {ind_ref}[0]"

    # Fallback to comment
    return f"False  # TODO: implement -> {cond}"


def _resolve_ref(token: str, indicators: list[Indicator]) -> str:
    """Map a token like 'ema_20' or 'rsi' to a self.xxx reference."""
    t = token.lower().replace("(", "_").replace(")", "")
    for ind in indicators:
        v = _var_name(ind)
        if t == v or t == ind.name.lower():
            return f"self.{v}"
    # Special tokens
    if t in ("price", "close"):
        return "self.data.close"
    if t in ("volume",):
        return "self.data.volume"
    return f"self.{t}"  # best guess


# ---------------------------------------------------------------------------
# Backtrader code generator
# ---------------------------------------------------------------------------

def generate_backtrader(bp: StrategyBlueprint) -> str:
    class_name = "".join(w.capitalize() for w in bp.name.split("_")) + "Strategy"

    # Indicator init lines
    ind_lines = "\n".join(_bt_indicator_line(ind) for ind in bp.indicators) or \
        "        pass  # No indicators defined"

    # Condition translation — each condition on its own line with inline comment
    def cond_block(conditions: list[str], label: str) -> str:
        if not conditions:
            return f"True  # no {label} conditions"
        parts = []
        for c in conditions:
            code = _translate_condition_bt(c, bp.indicators)
            parts.append(f"({code})  # {c}")
        return ("\n                and ").join(parts)

    long_conds  = cond_block(bp.long_conditions,  "Long entry")
    short_conds = cond_block(bp.short_conditions, "Short entry")
    exit_long   = cond_block(bp.exit_long_conditions,  "Exit long")
    exit_short  = cond_block(bp.exit_short_conditions, "Exit short")

    # Stop-loss / take-profit lines (use self.entry_price — called inside notify_order)
    sl_price = "self.entry_price * (1 - 0.02)"
    tp_price = "self.entry_price * (1 + 0.04)"
    if bp.stop_loss:
        if bp.stop_loss.type == "percent" and bp.stop_loss.value:
            sl_price = f"self.entry_price * (1 - {bp.stop_loss.value / 100:.4f})"
        elif bp.stop_loss.type == "atr" and bp.stop_loss.multiplier:
            sl_price = f"self.entry_price - self.atr[0] * {bp.stop_loss.multiplier}"
    if bp.take_profit:
        if bp.take_profit.type == "risk_reward" and bp.take_profit.ratio:
            tp_price = f"self.entry_price + (self.entry_price - self.sl_price) * {bp.take_profit.ratio}"
        elif bp.take_profit.type == "percent" and bp.take_profit.value:
            tp_price = f"self.entry_price * (1 + {bp.take_profit.value / 100:.4f})"

    # Position size
    size_pct = bp.position_sizing.value / 100 if bp.position_sizing else 0.05

    symbols_comment = ", ".join(bp.symbols)

    code = f'''\
"""
Auto-generated Backtrader strategy: {bp.name}
Asset class : {bp.asset_class}
Timeframe   : {bp.timeframe}
Symbols     : {symbols_comment}
Generated by: tradetest01
"""

import backtrader as bt
import backtrader.feeds as btfeeds
import backtrader.analyzers as btanalyzers
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path


class {class_name}(bt.Strategy):
    """
    {bp.notes or "Generated strategy — review conditions before live trading."}
    """

    params = dict(
        printlog=True,
        stake_pct={size_pct:.3f},
    )

    def log(self, txt, dt=None):
        if self.params.printlog:
            dt = dt or self.datas[0].datetime.date(0)
            print(f"{{dt.isoformat()}} {{txt}}")

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None
        self.entry_price = None
        self.sl_price = None
        self.tp_price = None

        # ----- Indicators -----
{ind_lines}

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        if order.status == order.Completed:
            side = "BUY" if order.isbuy() else "SELL"
            self.log(f"ORDER {{side}} @ {{order.executed.price:.4f}}, size={{order.executed.size:.4f}}")
            if order.isbuy():
                self.entry_price = order.executed.price
                self.sl_price = {sl_price}
                self.tp_price = {tp_price}
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log("Order Canceled/Margin/Rejected")
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f"TRADE CLOSED  Gross={{trade.pnl:.2f}}  Net={{trade.pnlcomm:.2f}}")

    def next(self):
        if self.order:
            return

        if not self.position:
            # ----- Long entry -----
            if (
                {long_conds}
            ):
                size = (self.broker.getvalue() * self.params.stake_pct) / self.dataclose[0]
                self.order = self.buy(size=size)

            # ----- Short entry -----
            elif (
                {short_conds}
            ):
                size = (self.broker.getvalue() * self.params.stake_pct) / self.dataclose[0]
                self.order = self.sell(size=size)

        else:
            # ----- Stop-loss / take-profit -----
            if self.entry_price:
                if self.position.size > 0:
                    if (self.dataclose[0] <= self.sl_price or
                            self.dataclose[0] >= self.tp_price):
                        self.order = self.close()
                        self.log(f"SL/TP hit @ {{self.dataclose[0]:.4f}}")
                        return

            # ----- Exit long -----
            if self.position.size > 0 and (
                {exit_long}
            ):
                self.order = self.close()

            # ----- Exit short -----
            elif self.position.size < 0 and (
                {exit_short}
            ):
                self.order = self.close()


# ---------------------------------------------------------------------------
# Mock data generator (sine-wave price series with noise)
# ---------------------------------------------------------------------------

def _generate_mock_ohlcv(bars: int = 500, start_price: float = 50000.0) -> pd.DataFrame:
    np.random.seed(42)
    t = np.linspace(0, 4 * np.pi, bars)
    trend = np.linspace(0, start_price * 0.2, bars)
    price = start_price + trend + start_price * 0.05 * np.sin(t) + \
            np.cumsum(np.random.randn(bars) * start_price * 0.003)

    opens  = price
    closes = price * (1 + np.random.randn(bars) * 0.002)
    highs  = np.maximum(opens, closes) * (1 + np.abs(np.random.randn(bars)) * 0.003)
    lows   = np.minimum(opens, closes) * (1 - np.abs(np.random.randn(bars)) * 0.003)
    vols   = np.abs(np.random.randn(bars) * 1000 + 5000)

    idx = pd.date_range("2023-01-01", periods=bars, freq="1h")
    return pd.DataFrame(dict(open=opens, high=highs, low=lows, close=closes, volume=vols), index=idx)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_backtest(
    data_path: str | None = None,
    cash: float = 100_000.0,
    commission: float = 0.001,
    plot: bool = False,
) -> dict:
    cerebro = bt.Cerebro()
    cerebro.addstrategy({class_name})

    if data_path:
        df = pd.read_csv(data_path, index_col=0, parse_dates=True)
    else:
        print("[mock] No data path provided — using synthetic OHLCV data.")
        df = _generate_mock_ohlcv()

    feed = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(feed)

    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=commission)

    cerebro.addanalyzer(btanalyzers.SharpeRatio, _name="sharpe", riskfreerate=0.0)
    cerebro.addanalyzer(btanalyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(btanalyzers.TradeAnalyzer, _name="trades")

    print(f"Starting Portfolio Value: {{cerebro.broker.getvalue():.2f}}")
    results = cerebro.run()
    strat = results[0]

    final_value = cerebro.broker.getvalue()
    pnl = final_value - cash
    print(f"Final Portfolio Value  : {{final_value:.2f}}")
    print(f"Net P&L                : {{pnl:+.2f}} ({{pnl/cash*100:+.2f}}%)")

    try:
        sharpe = strat.analyzers.sharpe.get_analysis().get("sharperatio", "N/A")
        dd = strat.analyzers.drawdown.get_analysis().get("max", {{}}).get("drawdown", "N/A")
        print(f"Sharpe Ratio           : {{sharpe}}")
        print(f"Max Drawdown           : {{dd}}%")
    except Exception:
        pass

    if plot:
        cerebro.plot(style="candlestick")

    return {{"pnl": pnl, "final_value": final_value}}


if __name__ == "__main__":
    run_backtest()
'''
    return code


# ---------------------------------------------------------------------------
# CCXT paper-trading code generator
# ---------------------------------------------------------------------------

def generate_ccxt(bp: StrategyBlueprint) -> str:
    class_name = "".join(w.capitalize() for w in bp.name.split("_")) + "LiveStrategy"
    symbol = bp.symbols[0] if bp.symbols else "BTC/USDT"
    size_pct = bp.position_sizing.value / 100 if bp.position_sizing else 0.05

    code = f'''\
"""
Auto-generated CCXT paper-trading strategy: {bp.name}
Asset class : {bp.asset_class}
Timeframe   : {bp.timeframe}
Symbol      : {symbol}
Generated by: tradetest01

Run with:  python strategies/output/{bp.name}_ccxt.py
"""

import time
import os
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()

def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def _bollinger(series: pd.Series, period: int = 20, std: float = 2.0):
    mid   = series.rolling(period).mean()
    sigma = series.rolling(period).std()
    return mid, mid + std * sigma, mid - std * sigma


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class {class_name}:
    def __init__(self, exchange: ccxt.Exchange, symbol: str = "{symbol}", timeframe: str = "{bp.timeframe}"):
        self.exchange  = exchange
        self.symbol    = symbol
        self.timeframe = timeframe
        self.position  = None   # None | dict
        self.cash      = float(os.getenv("PAPER_CASH", "10000"))
        self.stake_pct = {size_pct:.3f}
        self.log_entries: list[str] = []

    def _log(self, msg: str) -> None:
        ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{{ts}} {{msg}}"
        print(line)
        self.log_entries.append(line)

    def fetch_ohlcv(self, limit: int = 200) -> pd.DataFrame:
        raw = self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=limit)
        df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
'''
    # Add indicator calculations
    for ind in bp.indicators:
        name_up = ind.name.upper()
        period = ind.params.get("period", 14)
        var = _var_name(ind)
        if name_up == "EMA":
            code += f'        df["{var}"] = _ema(df["close"], {period})\n'
        elif name_up == "SMA":
            code += f'        df["{var}"] = _sma(df["close"], {period})\n'
        elif name_up == "RSI":
            code += f'        df["{var}"] = _rsi(df["close"], {period})\n'
        elif name_up == "ATR":
            code += f'        df["{var}"] = _atr(df, {period})\n'
        else:
            code += f'        # TODO: implement {ind.name} indicator\n'

    if not bp.indicators:
        code += "        pass  # No indicators\n"

    code += '''        return df

    def should_long(self, df: pd.DataFrame) -> bool:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        # Long entry conditions (implement by editing this method):
'''
    for c in (bp.long_conditions or ["# no long conditions extracted"]):
        code += f"        # {c}\n"
    code += "        return False  # TODO: implement conditions\n\n"

    code += '''    def should_short(self, df: pd.DataFrame) -> bool:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        # Short entry conditions (implement by editing this method):
'''
    for c in (bp.short_conditions or ["# no short conditions extracted"]):
        code += f"        # {c}\n"
    code += "        return False  # TODO: implement conditions\n\n"

    sl_pct = (bp.stop_loss.value / 100) if (bp.stop_loss and bp.stop_loss.type == "percent" and bp.stop_loss.value) else 0.02
    tp_ratio = (bp.take_profit.ratio) if (bp.take_profit and bp.take_profit.ratio) else 2.0

    code += f'''\
    def enter_long(self, price: float) -> None:
        size = (self.cash * self.stake_pct) / price
        sl   = price * (1 - {sl_pct:.4f})
        tp   = price + (price - sl) * {tp_ratio:.1f}
        self.position = dict(side="long", entry=price, size=size, sl=sl, tp=tp)
        self._log(f"ENTER LONG  @ {{price:.4f}}  size={{size:.6f}}  SL={{sl:.4f}}  TP={{tp:.4f}}")

    def enter_short(self, price: float) -> None:
        size = (self.cash * self.stake_pct) / price
        sl   = price * (1 + {sl_pct:.4f})
        tp   = price - (price - sl) * {tp_ratio:.1f}  # Note: sl > entry for short
        self.position = dict(side="short", entry=price, size=size, sl=sl, tp=tp)
        self._log(f"ENTER SHORT @ {{price:.4f}}  size={{size:.6f}}  SL={{sl:.4f}}  TP={{tp:.4f}}")

    def check_exit(self, price: float) -> None:
        if not self.position:
            return
        p = self.position
        hit_sl = (p["side"] == "long"  and price <= p["sl"]) or \\
                 (p["side"] == "short" and price >= p["sl"])
        hit_tp = (p["side"] == "long"  and price >= p["tp"]) or \\
                 (p["side"] == "short" and price <= p["tp"])
        if hit_sl or hit_tp:
            reason = "SL" if hit_sl else "TP"
            if p["side"] == "long":
                pnl = (price - p["entry"]) * p["size"]
            else:
                pnl = (p["entry"] - price) * p["size"]
            self.cash += pnl
            self._log(f"EXIT {{reason}} @ {{price:.4f}}  PnL={{pnl:+.2f}}  Cash={{self.cash:.2f}}")
            self.position = None

    def run(self, max_iterations: int = 0) -> None:
        self._log(f"Strategy started. Symbol={{self.symbol}} TF={{self.timeframe}} Cash={{self.cash:.2f}}")
        iterations = 0
        try:
            while True:
                df = self.fetch_ohlcv()
                df = self.calculate_indicators(df)
                price = float(df["close"].iloc[-1])

                self.check_exit(price)

                if not self.position:
                    if self.should_long(df):
                        self.enter_long(price)
                    elif self.should_short(df):
                        self.enter_short(price)
                else:
                    self._log(f"HOLDING {{self.position['side'].upper()}}  entry={{self.position['entry']:.4f}}  current={{price:.4f}}")

                iterations += 1
                if max_iterations and iterations >= max_iterations:
                    break

                tf_seconds = _timeframe_to_seconds("{bp.timeframe}")
                time.sleep(tf_seconds)

        except KeyboardInterrupt:
            self._log("Strategy stopped by user.")
        finally:
            self._log(f"Final Cash: {{self.cash:.2f}}")


def _timeframe_to_seconds(tf: str) -> int:
    mapping = {{"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}}
    return mapping.get(tf, 3600)


def build_exchange(sandbox: bool = True) -> ccxt.Exchange:
    name = os.getenv("EXCHANGE_NAME", "binance")
    ex   = getattr(ccxt, name)({{
        "apiKey":  os.getenv("EXCHANGE_API_KEY", ""),
        "secret":  os.getenv("EXCHANGE_SECRET", ""),
    }})
    if sandbox and hasattr(ex, "set_sandbox_mode"):
        ex.set_sandbox_mode(True)
    return ex


if __name__ == "__main__":
    exchange = build_exchange(sandbox=True)
    strategy = {class_name}(exchange)
    strategy.run()
'''
    return code


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compile_strategy(name: str, mode: str = "backtrader") -> Path:
    from db import get_blueprint
    from parser.schema import StrategyBlueprint

    data = get_blueprint(name)
    if data is None:
        raise ValueError(f"Strategy '{name}' not found in database. Run `parse` first.")

    bp = StrategyBlueprint.model_validate(data)

    if mode == "backtrader":
        code = generate_backtrader(bp)
        out  = STRATEGIES_DIR / f"{name}_backtrader.py"
    elif mode == "ccxt":
        code = generate_ccxt(bp)
        out  = STRATEGIES_DIR / f"{name}_ccxt.py"
    else:
        raise ValueError(f"Unknown mode '{mode}'. Use 'backtrader' or 'ccxt'.")

    out.write_text(code, encoding="utf-8")
    return out
