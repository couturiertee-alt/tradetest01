"""
Mock paper-trading engine — runs a compiled CCXT strategy against synthetic OHLCV data.
Used when no exchange credentials are configured.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import STRATEGIES_DIR

console = Console()


# ---------------------------------------------------------------------------
# Synthetic exchange
# ---------------------------------------------------------------------------

class MockExchange:
    """Minimal CCXT-compatible mock that streams synthetic price data."""

    def __init__(self, symbol: str = "BTC/USDT", start_price: float = 50_000.0, bars: int = 500):
        self.symbol      = symbol
        self._df         = _generate_ohlcv(bars, start_price)
        self._cursor     = 50  # start after warm-up period

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> list:
        end   = self._cursor
        start = max(0, end - limit)
        chunk = self._df.iloc[start:end]
        self._cursor = min(self._cursor + 1, len(self._df))

        result = []
        for ts, row in chunk.iterrows():
            result.append([
                int(ts.timestamp() * 1000),
                row["open"], row["high"], row["low"], row["close"], row["volume"],
            ])
        return result

    @property
    def more_data(self) -> bool:
        return self._cursor < len(self._df)


def _generate_ohlcv(bars: int = 500, start_price: float = 50_000.0) -> pd.DataFrame:
    np.random.seed(42)
    t      = np.linspace(0, 6 * np.pi, bars)
    trend  = np.linspace(0, start_price * 0.15, bars)
    price  = start_price + trend + start_price * 0.06 * np.sin(t) + \
             np.cumsum(np.random.randn(bars) * start_price * 0.003)
    price  = np.maximum(price, start_price * 0.1)

    opens  = price
    closes = price * (1 + np.random.randn(bars) * 0.002)
    highs  = np.maximum(opens, closes) * (1 + np.abs(np.random.randn(bars)) * 0.003)
    lows   = np.minimum(opens, closes) * (1 - np.abs(np.random.randn(bars)) * 0.003)
    vols   = np.abs(np.random.randn(bars) * 1_000 + 5_000)

    idx = pd.date_range("2023-01-01", periods=bars, freq="1h")
    return pd.DataFrame(dict(open=opens, high=highs, low=lows, close=closes, volume=vols), index=idx)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_paper_trade(name: str, speed: float = 0.05) -> None:
    """
    Loads a compiled CCXT strategy and runs it against MockExchange.
    Renders a live Rich dashboard.

    Args:
        name:  strategy name (must be compiled first)
        speed: seconds between ticks (0 = as fast as possible)
    """
    strategy_file = STRATEGIES_DIR / f"{name}_ccxt.py"
    if not strategy_file.exists():
        console.print(f"[red]No compiled CCXT strategy found at {strategy_file}[/red]")
        console.print(f"[yellow]Run: python main.py compile {name} --mode ccxt[/yellow]")
        return

    # Dynamically load the generated strategy module
    spec   = importlib.util.spec_from_file_location(f"strategy_{name}", strategy_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"strategy_{name}"] = module
    spec.loader.exec_module(module)

    # Find the strategy class (ends with LiveStrategy or Strategy)
    strategy_cls = None
    for attr in dir(module):
        obj = getattr(module, attr)
        if isinstance(obj, type) and attr.endswith(("Strategy",)) and attr != "type":
            strategy_cls = obj
            break

    if strategy_cls is None:
        console.print("[red]Could not find a strategy class in the compiled file.[/red]")
        return

    symbol = "BTC/USDT"
    exchange = MockExchange(symbol=symbol)
    strategy = strategy_cls(exchange=exchange, symbol=symbol)  # type: ignore[call-arg]

    trades:   list[dict] = []
    log_lines: list[str] = []

    # Monkey-patch _log to capture output
    original_log = strategy._log
    def capture_log(msg: str) -> None:
        original_log(msg)
        log_lines.append(msg)
    strategy._log = capture_log  # type: ignore[method-assign]

    # Monkey-patch position tracking to capture trades
    orig_enter_long  = strategy.enter_long
    orig_enter_short = strategy.enter_short
    orig_check_exit  = strategy.check_exit

    def tracked_enter_long(price: float) -> None:
        orig_enter_long(price)
        trades.append({"type": "LONG", "entry": price, "exit": None, "pnl": None})

    def tracked_enter_short(price: float) -> None:
        orig_enter_short(price)
        trades.append({"type": "SHORT", "entry": price, "exit": None, "pnl": None})

    def tracked_check_exit(price: float) -> None:
        had_pos = strategy.position is not None
        orig_check_exit(price)
        if had_pos and strategy.position is None and trades:
            last = trades[-1]
            last["exit"] = price
            if last["type"] == "LONG":
                last["pnl"] = (price - last["entry"]) * (strategy.cash * strategy.stake_pct / last["entry"])
            else:
                last["pnl"] = (last["entry"] - price) * (strategy.cash * strategy.stake_pct / last["entry"])

    strategy.enter_long  = tracked_enter_long   # type: ignore[method-assign]
    strategy.enter_short = tracked_enter_short  # type: ignore[method-assign]
    strategy.check_exit  = tracked_check_exit   # type: ignore[method-assign]

    console.print(Panel(f"[bold green]Paper Trading: {name}[/bold green]\n"
                        f"Symbol: {symbol}  |  Press Ctrl+C to stop",
                        title="tradetest01"))

    def make_table() -> Table:
        tbl = Table(title="Trade Log", expand=True, border_style="dim")
        tbl.add_column("#",      style="dim",    width=4)
        tbl.add_column("Side",   style="cyan",   width=6)
        tbl.add_column("Entry",  style="yellow", width=12)
        tbl.add_column("Exit",   style="yellow", width=12)
        tbl.add_column("PnL",    width=12)
        for i, t in enumerate(trades[-15:], 1):
            pnl_str = f"{t['pnl']:+.2f}" if t["pnl"] is not None else "open"
            color   = "green" if (t.get("pnl") or 0) > 0 else "red"
            tbl.add_row(
                str(i),
                t["type"],
                f"{t['entry']:.2f}",
                f"{t['exit']:.2f}" if t["exit"] else "—",
                Text(pnl_str, style=color),
            )
        return tbl

    def make_status() -> Panel:
        pos_str = "FLAT"
        if strategy.position:
            p = strategy.position
            pos_str = f"{p['side'].upper()}  entry={p['entry']:.2f}  SL={p['sl']:.2f}  TP={p['tp']:.2f}"
        total_pnl = sum(t["pnl"] for t in trades if t.get("pnl") is not None)
        color = "green" if total_pnl >= 0 else "red"
        last_log = log_lines[-1] if log_lines else "—"
        text = (
            f"[bold]Cash:[/bold] {strategy.cash:.2f}  "
            f"[bold]Total PnL:[/bold] [{color}]{total_pnl:+.2f}[/{color}]  "
            f"[bold]Trades:[/bold] {len(trades)}\n"
            f"[bold]Position:[/bold] {pos_str}\n"
            f"[dim]{last_log}[/dim]"
        )
        return Panel(text, title="Status", border_style="blue")

    try:
        with Live(console=console, refresh_per_second=4) as live:
            iterations = 0
            while exchange.more_data:
                df = strategy.fetch_ohlcv()
                df = strategy.calculate_indicators(df)
                price = float(df["close"].iloc[-1])

                strategy.check_exit(price)

                if not strategy.position:
                    if strategy.should_long(df):
                        strategy.enter_long(price)
                    elif strategy.should_short(df):
                        strategy.enter_short(price)

                iterations += 1
                from rich.columns import Columns
                live.update(Columns([make_status(), make_table()]))
                if speed > 0:
                    time.sleep(speed)

    except KeyboardInterrupt:
        pass

    # Final summary
    total_pnl   = sum(t["pnl"] for t in trades if t.get("pnl") is not None)
    closed       = [t for t in trades if t.get("pnl") is not None]
    wins         = [t for t in closed if t["pnl"] > 0]
    win_rate     = len(wins) / len(closed) * 100 if closed else 0

    console.print(Panel(
        f"[bold]Simulation Complete[/bold]\n"
        f"Total Trades  : {len(trades)}\n"
        f"Closed Trades : {len(closed)}\n"
        f"Win Rate      : {win_rate:.1f}%\n"
        f"Net PnL       : {'[green]' if total_pnl >= 0 else '[red]'}{total_pnl:+.2f}[/]\n"
        f"Final Cash    : {strategy.cash:.2f}",
        title="Results",
        border_style="green" if total_pnl >= 0 else "red",
    ))

    return {"pnl": total_pnl, "trades": len(trades), "log": "\n".join(log_lines)}
