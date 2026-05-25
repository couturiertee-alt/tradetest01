#!/usr/bin/env python3
"""
backtest_arb.py  ─  USDT/USDC Cross-Pair Arbitrage Historical Backtest

Data source  : Bybit Spot public API via CCXT (no API keys).
               Falls back to realistic synthetic data (GBM + OU spread)
               if the network is unavailable.

Strategy     : Same rules as arbitrage_sim.py, tested on real price history.
  1. Spread : close(USDT pair) − close(USDC pair) > threshold
  2. Depth  : bullish candle on USDC pair (close > open) as buy-pressure proxy
  3. Entry  : USDC pair open of the *next* bar (no lookahead bias)
  4. Exit   : +3% take-profit  |  −1% stop-loss  |  24-bar timeout

Run:
    python backtest_arb.py            # fetch live data, 90 days, 1h bars
    python backtest_arb.py --days 30
    python backtest_arb.py --synth    # force synthetic data
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import ccxt
import numpy as np
import pandas as pd
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

ASSETS: list[tuple[str, float]] = [
    ("BTC",  65_000), ("ETH",   3_200), ("SOL",    185),
    ("ARB",    1.23), ("AVAX",   38.5), ("LINK",  18.2),
    ("MATIC",  0.87), ("DOT",    9.20), ("ADA",   0.46),
    ("UNI",   12.10),
]

TIMEFRAME     = "1h"
DAYS          = int(next((sys.argv[sys.argv.index("--days") + 1]
                          for _ in ["x"] if "--days" in sys.argv), 90))
FORCE_SYNTH   = "--synth" in sys.argv

TARGET_PCT    = 0.030   # +3.0%  take-profit
STOP_PCT      = 0.010   # −1.0%  stop-loss
TIMEOUT_BARS  = 24      # 24 bars (24 h on 1h TF) before forced exit
ALLOCATION    = 5_000.0 # USDT per trade
COMMISSION    = 0.001   # 0.1% per leg (0.2% round-trip)
SPREAD_THRESH = 0.0001  # minimum spread fraction (0.01%)

# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    asset:       str
    entry_time:  datetime
    entry_price: float
    exit_time:   datetime
    exit_price:  float
    outcome:     str    # WIN | LOSS | TIMEOUT_WIN | TIMEOUT_LOSS
    pnl:         float  # USDT
    pnl_pct:     float  # percent
    bars_held:   int


# ─────────────────────────────────────────────────────────────────────────────
# Data Fetching
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_one(ex: ccxt.Exchange, symbol: str) -> Optional[pd.DataFrame]:
    try:
        limit = min(DAYS * 24 + 1, 1000)
        since = int((datetime.utcnow() - timedelta(days=DAYS)).timestamp() * 1000)
        raw   = ex.fetch_ohlcv(symbol, TIMEFRAME, since=since, limit=limit)
        if not raw:
            return None
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df.sort_index()
    except Exception:
        return None


def fetch_pair_live(asset: str) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    """Try Bybit public spot API; returns (usdt_df, usdc_df) or (None, None)."""
    try:
        ex = ccxt.bybit({
            "options": {"defaultType": "spot"},
            "enableRateLimit": True,
        })
        usdt = _fetch_one(ex, f"{asset}/USDT")
        usdc = _fetch_one(ex, f"{asset}/USDC")
        return usdt, usdc
    except Exception:
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic Data Generator
# Uses GBM for price and an Ornstein-Uhlenbeck process for the USDT/USDC spread.
# ─────────────────────────────────────────────────────────────────────────────

def generate_synthetic(asset: str, base_price: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Generate correlated USDT/USDC OHLCV data.

    Model
    ─────
    Price  : Geometric Brownian Motion (σ ≈ 45% annual = 0.64% hourly)
    Spread : Ornstein-Uhlenbeck mean-reverting to 0, with occasional spikes
             (simulates transient order-book imbalances that drive the arb signal)
    """
    # Deterministic seed from asset name bytes (avoids Python's per-process hash randomisation)
    rng = np.random.default_rng(42 + sum(b for b in asset.encode()))
    n   = DAYS * 24
    dt  = 1.0 / (365 * 24)

    # ── GBM price ──────────────────────────────────────────────────────────
    sigma   = 0.45
    returns = rng.normal(0.0, sigma * np.sqrt(dt), n)
    price   = base_price * np.exp(np.cumsum(returns))

    # ── OU spread ──────────────────────────────────────────────────────────
    # dX = θ(μ − X)dt + σ dW
    theta       = 2.0                             # fast mean-reversion
    sigma_ou    = base_price * 0.0018             # spread volatility
    spread      = np.zeros(n)
    for i in range(1, n):
        spread[i] = (spread[i-1]
                     + theta * (0.0 - spread[i-1]) * dt
                     + sigma_ou * rng.normal() * np.sqrt(dt))
        # ~2% chance per bar of a spread spike (simulated order-flow event)
        if rng.random() < 0.02:
            spread[i] += rng.choice([-1, 1]) * base_price * rng.uniform(0.001, 0.004)

    usdt_close = price + spread * 0.5
    usdc_close = price - spread * 0.5

    def _build_ohlcv(close: np.ndarray) -> pd.DataFrame:
        intra = np.abs(rng.normal(0, sigma * np.sqrt(dt), n))
        opens = np.concatenate([[close[0]], close[:-1]])   # previous close = next open
        highs = np.maximum(opens, close) * (1 + intra)
        lows  = np.minimum(opens, close) * (1 - intra)
        vol   = np.abs(rng.lognormal(np.log(5e5 / base_price), 0.6, n))
        idx   = pd.date_range(
            end=pd.Timestamp.now("UTC").floor("h"),
            periods=n, freq="1h", tz="UTC",
        )
        return pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": close, "volume": vol},
            index=idx,
        )

    return _build_ohlcv(usdt_close), _build_ohlcv(usdc_close)


# ─────────────────────────────────────────────────────────────────────────────
# Bar-by-Bar Backtest Engine
# ─────────────────────────────────────────────────────────────────────────────

def backtest_pair(
    usdt: pd.DataFrame,
    usdc: pd.DataFrame,
    asset: str,
) -> list[Trade]:
    """
    Run bar-by-bar backtest on a single asset.

    Entry rule (end of bar i):
      spread  = (usdt.close[i] − usdc.close[i]) / usdc.close[i] > SPREAD_THRESH
      depth   = usdc.close[i] > usdc.open[i]   (bullish candle proxy)
      → enter at usdc.open[i+1]

    Exit rule (bars i+1 … i+TIMEOUT_BARS):
      target_price = entry * (1 + TARGET_PCT)
      stop_price   = entry * (1 − STOP_PCT)

      If bar high ≥ target AND bar low ≤ stop:
          bearish bar (close < open) → stop first → LOSS
          bullish bar               → target first → WIN
      If bar high ≥ target only     → WIN  at target
      If bar low  ≤ stop  only      → LOSS at stop
      After TIMEOUT_BARS            → exit at bar close (TIMEOUT_WIN/LOSS)
    """
    idx   = usdt.index.intersection(usdc.index).sort_values()
    ut    = usdt.loc[idx]
    uc    = usdc.loc[idx]
    n     = len(idx)
    trades: list[Trade] = []

    i = 0
    while i < n - TIMEOUT_BARS - 1:
        # ── Entry check ──────────────────────────────────────────────────
        spread_frac = (ut["close"].iat[i] - uc["close"].iat[i]) / uc["close"].iat[i]
        depth_ok    = uc["close"].iat[i] > uc["open"].iat[i]

        if spread_frac <= SPREAD_THRESH or not depth_ok:
            i += 1
            continue

        # Enter at next bar's open (USDC pair = cheaper side)
        entry_price = uc["open"].iat[i + 1]
        entry_time  = idx[i + 1]
        target      = entry_price * (1.0 + TARGET_PCT)
        stop        = entry_price * (1.0 - STOP_PCT)

        # ── Exit scan ────────────────────────────────────────────────────
        outcome     = None
        exit_price  = None
        exit_bar    = None

        for j in range(i + 1, min(i + 1 + TIMEOUT_BARS, n)):
            # Use USDT pair highs/lows for exit (that's where we sell)
            bar_high  = ut["high"].iat[j]
            bar_low   = ut["low"].iat[j]
            bar_close = ut["close"].iat[j]
            bar_open  = ut["open"].iat[j]

            hit_target = bar_high  >= target
            hit_stop   = bar_low   <= stop

            if hit_target and hit_stop:
                # Both levels touched in the same bar — infer sequence from direction
                if bar_close >= bar_open:   # bullish → assume target hit first
                    outcome, exit_price = "WIN",  target
                else:                       # bearish → assume stop hit first
                    outcome, exit_price = "LOSS", stop
                exit_bar = j
                break
            elif hit_target:
                outcome, exit_price = "WIN",  target
                exit_bar = j
                break
            elif hit_stop:
                outcome, exit_price = "LOSS", stop
                exit_bar = j
                break

        if outcome is None:
            # Timeout — exit at close of last checked bar
            j = min(i + TIMEOUT_BARS, n - 1)
            exit_price = ut["close"].iat[j]
            exit_bar   = j
            outcome = "TIMEOUT_WIN" if exit_price >= entry_price else "TIMEOUT_LOSS"

        raw_ret  = (exit_price - entry_price) / entry_price
        net_ret  = raw_ret - 2 * COMMISSION
        pnl      = ALLOCATION * net_ret

        trades.append(Trade(
            asset       = asset,
            entry_time  = entry_time,
            entry_price = entry_price,
            exit_time   = idx[exit_bar],
            exit_price  = exit_price,
            outcome     = outcome,
            pnl         = pnl,
            pnl_pct     = net_ret * 100,
            bars_held   = exit_bar - (i + 1),
        ))

        # Skip forward past this trade to avoid overlapping positions
        i = exit_bar + 1

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(trades: list[Trade]) -> dict:
    if not trades:
        return {}

    pnls        = np.array([t.pnl     for t in trades])
    pnl_pcts    = np.array([t.pnl_pct for t in trades])
    wins        = [t for t in trades if "WIN"  in t.outcome]
    losses      = [t for t in trades if "LOSS" in t.outcome]
    timeouts    = [t for t in trades if "TIMEOUT" in t.outcome]

    win_rate    = len(wins) / len(trades)
    avg_win     = float(np.mean([t.pnl for t in wins]))   if wins   else 0.0
    avg_loss    = float(np.mean([t.pnl for t in losses])) if losses else 0.0
    rr          = abs(avg_win / avg_loss) if avg_loss else float("inf")
    expectancy  = win_rate * avg_win + (1 - win_rate) * avg_loss

    cumul       = np.cumsum(pnls)
    peak        = np.maximum.accumulate(cumul)
    drawdown    = cumul - peak
    max_dd      = float(np.min(drawdown))
    max_dd_pct  = (max_dd / ALLOCATION) * 100 if ALLOCATION else 0

    std = pnl_pcts.std()
    sharpe = (pnl_pcts.mean() / std * np.sqrt(365 * 24)) if std > 0 else 0.0

    profit_factor = (
        sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))
        if losses else float("inf")
    )

    return dict(
        total         = len(trades),
        wins          = len(wins),
        losses        = len([t for t in losses if "TIMEOUT" not in t.outcome]),
        timeouts      = len(timeouts),
        win_rate      = win_rate,
        avg_win       = avg_win,
        avg_loss      = avg_loss,
        rr            = rr,
        expectancy    = expectancy,
        total_pnl     = float(pnls.sum()),
        max_dd        = max_dd,
        max_dd_pct    = max_dd_pct,
        sharpe        = sharpe,
        profit_factor = profit_factor,
        avg_hold      = float(np.mean([t.bars_held for t in trades])),
        cumul         = cumul.tolist(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# ASCII Equity Curve
# ─────────────────────────────────────────────────────────────────────────────

def _sparkline(values: list[float], width: int = 68, height: int = 10) -> str:
    """Render a block-character equity curve. Uses | not │ to avoid Rich border conflicts."""
    if not values:
        return ""
    step = max(1, len(values) // width)
    pts  = values[::step][:width]
    lo   = min(0.0, min(pts))
    hi   = max(0.0, max(pts))
    span = hi - lo if hi != lo else 1.0

    # Build a grid: grid[row][col] = char
    grid = [[" "] * len(pts) for _ in range(height)]
    for col, v in enumerate(pts):
        filled = int((v - lo) / span * (height - 1))
        for row in range(filled + 1):
            grid[row][col] = "█" if row < filled else "▄"

    rows = []
    for row in range(height - 1, -1, -1):
        label = f"{lo + span * row / (height - 1):>+9,.0f}"
        rows.append(f"{label} |{''.join(grid[row])}")

    rows.append("          +" + "─" * len(pts))
    start_lbl = "Start"
    end_lbl   = "End"
    pad       = len(pts) - len(start_lbl) - len(end_lbl)
    rows.append(f"           {start_lbl}{' ' * max(pad, 1)}{end_lbl}")
    return "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Rich Report
# ─────────────────────────────────────────────────────────────────────────────

def render_report(
    results:     dict[str, list[Trade]],
    data_source: str,
    period_days: int,
) -> None:
    all_trades = [t for ts in results.values() for t in ts]
    overall    = compute_metrics(all_trades)

    if not overall:
        console.print("[red]No trades generated — check spread threshold or data.[/red]")
        return

    # ── Header ───────────────────────────────────────────────────────────────
    wr_col = "green" if overall["win_rate"] >= 0.5 else "red"
    pnl_col = "green" if overall["total_pnl"] >= 0 else "red"

    console.print(Panel(
        f"  [bold cyan]USDT/USDC Arb Strategy — Historical Backtest[/bold cyan]\n\n"
        f"  Data source  : [yellow]{data_source}[/yellow]\n"
        f"  Period       : {period_days} days  ·  {TIMEFRAME} candles\n"
        f"  Assets tested: {len(results)}\n"
        f"  Parameters   : Target [green]+{TARGET_PCT*100:.0f}%[/]  "
        f"Stop [red]−{STOP_PCT*100:.0f}%[/]  "
        f"Timeout {TIMEOUT_BARS}h  "
        f"Allocation ${ALLOCATION:,.0f}",
        title="[bold cyan]Backtest Report[/bold cyan]",
        border_style="cyan",
    ))

    # ── Portfolio Summary ─────────────────────────────────────────────────────
    summary = Table(title="Portfolio Summary", box=box.SIMPLE_HEAD, border_style="cyan")
    summary.add_column("Metric",       style="dim",  width=22)
    summary.add_column("Value",        style="bold", justify="right", width=16)
    summary.add_column("Metric",       style="dim",  width=22)
    summary.add_column("Value",        style="bold", justify="right", width=16)

    def _fmt_pct(v: float, good_positive: bool = True) -> Text:
        col = ("green" if v >= 0 else "red") if good_positive else ("red" if v >= 0 else "green")
        return Text(f"{v:+.2f}%", style=col)

    rows = [
        ("Total Trades",   str(overall["total"]),
         "Profit Factor",  f"{overall['profit_factor']:.2f}"),

        ("Wins",           f"{overall['wins']}  ({overall['win_rate']*100:.1f}%)",
         "Avg Win",        f"+${overall['avg_win']:,.2f}"),

        ("Losses",         str(overall["losses"]),
         "Avg Loss",       f"−${abs(overall['avg_loss']):,.2f}"),

        ("Timeouts",       str(overall["timeouts"]),
         "Risk:Reward",    f"1 : {overall['rr']:.2f}"),

        ("Avg Hold (h)",   f"{overall['avg_hold']:.1f}",
         "Expectancy/tr.", f"${overall['expectancy']:+,.2f}"),

        ("Sharpe Ratio",   f"{overall['sharpe']:.3f}",
         "Max Drawdown",   f"${overall['max_dd']:,.2f}  ({overall['max_dd_pct']:+.1f}%)"),
    ]
    for r in rows:
        summary.add_row(*r)

    total_pnl_text = Text(f"${overall['total_pnl']:+,.2f} USDT", style=pnl_col + " bold")
    summary.add_row("─" * 20, "─" * 14, "Total Net PnL", "")
    summary.add_row("", "", "", total_pnl_text)
    console.print(summary)

    # ── Per-asset breakdown ───────────────────────────────────────────────────
    asset_tbl = Table(title="Per-Asset Breakdown", box=box.SIMPLE_HEAD, border_style="dim")
    asset_tbl.add_column("Asset",    style="bold",  width=7)
    asset_tbl.add_column("Trades",   justify="right", width=7)
    asset_tbl.add_column("Wins",     justify="right", width=7)
    asset_tbl.add_column("Win %",    justify="right", width=8)
    asset_tbl.add_column("Avg Win",  justify="right", width=10)
    asset_tbl.add_column("Avg Loss", justify="right", width=10)
    asset_tbl.add_column("R:R",      justify="right", width=6)
    asset_tbl.add_column("Net PnL",  justify="right", width=12)
    asset_tbl.add_column("Sharpe",   justify="right", width=8)

    for asset, trades in sorted(results.items(), key=lambda x: -sum(t.pnl for t in x[1])):
        if not trades:
            continue
        m   = compute_metrics(trades)
        pc  = "green" if m["total_pnl"] >= 0 else "red"
        wrc = "green" if m["win_rate"] >= 0.5 else "red"
        asset_tbl.add_row(
            asset,
            str(m["total"]),
            str(m["wins"]),
            Text(f"{m['win_rate']*100:.1f}%", style=wrc),
            f"+${m['avg_win']:,.2f}",
            f"−${abs(m['avg_loss']):,.2f}",
            f"{m['rr']:.2f}",
            Text(f"{m['total_pnl']:+,.2f}", style=pc),
            f"{m['sharpe']:.2f}",
        )
    console.print(asset_tbl)

    # ── Outcome Distribution ──────────────────────────────────────────────────
    outcomes = {"WIN": 0, "LOSS": 0, "TIMEOUT_WIN": 0, "TIMEOUT_LOSS": 0}
    for t in all_trades:
        outcomes[t.outcome] = outcomes.get(t.outcome, 0) + 1

    dist_tbl = Table(title="Outcome Distribution", box=box.SIMPLE_HEAD, border_style="dim")
    dist_tbl.add_column("Outcome",    style="bold", width=16)
    dist_tbl.add_column("Count",      justify="right", width=8)
    dist_tbl.add_column("Share",      justify="right", width=8)
    dist_tbl.add_column("Bar",        width=30)
    total_t = len(all_trades)
    colors  = {"WIN": "green", "LOSS": "red", "TIMEOUT_WIN": "cyan", "TIMEOUT_LOSS": "yellow"}
    for outcome, count in outcomes.items():
        if count == 0:
            continue
        share = count / total_t
        bar   = "█" * int(share * 28)
        dist_tbl.add_row(
            Text(outcome, style=colors.get(outcome, "white")),
            str(count),
            f"{share*100:.1f}%",
            Text(bar, style=colors.get(outcome, "white")),
        )
    console.print(dist_tbl)

    # ── Equity Curve ──────────────────────────────────────────────────────────
    if overall["cumul"]:
        curve = _sparkline(overall["cumul"], width=70, height=9)
        console.print(Panel(
            curve,
            title=f"[bold]Equity Curve  (cumulative PnL over {overall['total']} trades)[/bold]",
            border_style="dim cyan",
        ))

    # ── Verdict ───────────────────────────────────────────────────────────────
    exp    = overall["expectancy"]
    sharpe = overall["sharpe"]
    wr     = overall["win_rate"]
    rr     = overall["rr"]

    if exp > 0 and sharpe > 0.5:
        verdict = "[green bold]POSITIVE EDGE[/]  — positive expectancy + adequate Sharpe"
        advice  = "Strategy shows measurable edge on this data. Consider position sizing and live testing with small allocation."
    elif exp > 0 and sharpe <= 0.5:
        verdict = "[yellow bold]MARGINAL EDGE[/]  — positive expectancy but high variance"
        advice  = "Expectancy is positive but Sharpe is low. High variance relative to returns — reduce position size or tighten stop."
    elif exp <= 0 and rr >= 2:
        verdict = "[yellow bold]LOW WIN RATE[/]  — R:R is fine but too few winners"
        advice  = f"Win rate ({wr*100:.1f}%) is below breakeven for a {rr:.1f}:1 R:R. Consider relaxing depth filter or entry conditions."
    else:
        verdict = "[red bold]NO EDGE DETECTED[/]  — negative expectancy"
        advice  = "Strategy loses money on this dataset. The spread + depth signals do not predict +3% moves reliably. Add a trend filter or reduce target."

    console.print(Panel(
        f"  {verdict}\n\n"
        f"  Win Rate   : [{wr_col}]{wr*100:.1f}%[/{wr_col}]  "
        f"(breakeven at {1/(1+rr)*100:.1f}% for {rr:.1f}:1 R:R)\n"
        f"  Expectancy : [{pnl_col}]${exp:+,.2f} per trade[/{pnl_col}]\n"
        f"  Sharpe     : {sharpe:.3f}\n\n"
        f"  [dim]{advice}[/dim]",
        title="[bold]Strategy Verdict[/bold]",
        border_style="cyan",
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    console.print(Panel.fit(
        f"[bold cyan]USDT/USDC Arb Backtest[/bold cyan]  "
        f"[dim]│[/dim]  {DAYS}d  {TIMEFRAME}  "
        f"[dim]│[/dim]  Target [green]+{TARGET_PCT*100:.0f}%[/]  "
        f"Stop [red]−{STOP_PCT*100:.0f}%[/]  "
        f"Timeout {TIMEOUT_BARS}h  "
        f"[dim]│[/dim]  {len(ASSETS)} assets",
        border_style="cyan",
    ))

    results:     dict[str, list[Trade]] = {}
    data_source  = "Bybit Spot (live)"
    live_ok      = False

    if not FORCE_SYNTH:
        console.print("[dim]Attempting Bybit public API…[/dim]", end=" ")
        # Quick connectivity probe
        try:
            ex    = ccxt.bybit({"options": {"defaultType": "spot"}, "enableRateLimit": True})
            probe = _fetch_one(ex, "BTC/USDT")
            live_ok = probe is not None and len(probe) > 10
        except Exception:
            live_ok = False
        console.print("[green]connected[/green]" if live_ok else "[yellow]offline — using synthetic data[/yellow]")

    if not live_ok:
        data_source = f"Synthetic (GBM + OU spread, {DAYS}d)"

    for asset, base_price in ASSETS:
        console.print(f"  [dim]{asset}[/dim]", end=" ")

        if live_ok:
            usdt_df, usdc_df = fetch_pair_live(asset)
            if usdt_df is None or usdc_df is None or len(usdt_df) < 50:
                console.print("[yellow](live data unavailable — using synthetic)[/yellow]", end=" ")
                usdt_df, usdc_df = generate_synthetic(asset, base_price)
                if data_source == "Bybit Spot (live)":
                    data_source = "Bybit Spot (live) + synthetic fallback"
        else:
            usdt_df, usdc_df = generate_synthetic(asset, base_price)

        trades = backtest_pair(usdt_df, usdc_df, asset)
        results[asset] = trades
        pnl = sum(t.pnl for t in trades)
        col = "green" if pnl >= 0 else "red"
        console.print(
            f"[dim]{len(trades)} trades[/dim]  "
            f"[{col}]{pnl:+,.2f} USDT[/{col}]"
        )
        if live_ok:
            time.sleep(0.3)   # gentle rate limiting

    console.print()
    render_report(results, data_source, DAYS)


if __name__ == "__main__":
    main()
