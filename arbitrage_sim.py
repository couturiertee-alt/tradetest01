#!/usr/bin/env python3
"""
arbitrage_sim.py  ─  USDT/USDC Cross-Pair Arbitrage Scanner & Simulator

Strategy (from transcript)
──────────────────────────
1. MARKET SCAN  : Find Bybit Spot assets listed in both X/USDT and X/USDC
                  Volume gate: ≥$5M 24h vol (live) or mock gate.
2. SPREAD DETECT: Flag when Ask(USDT pair) − Bid(USDC pair) > 0
3. DEPTH GATE   : Within ±2% of mid-price, Buy Depth > Sell Depth
4. EXECUTION    : Convert USDT→USDC  →  BUY at USDC bid
                  Set SELL at +3% on USDT pair  →  track until fill or 1h timeout

Run modes:
    python arbitrage_sim.py                    # mock order books, simulated fills
    python arbitrage_sim.py --fast             # mock + accelerated fill times
    python arbitrage_sim.py --live             # real Bybit order books, simulated fills
    python arbitrage_sim.py --live --execute   # real order books + real order placement
    python arbitrage_sim.py --live --sandbox   # real Bybit TESTNET (default when --live)
    python arbitrage_sim.py --test N           # headless N-cycle test
    python arbitrage_sim.py --no-screen        # disable Rich Live screen

Credentials (for --execute):
    Set EXCHANGE_API_KEY and EXCHANGE_SECRET in .env
    EXCHANGE_SANDBOX=false  to trade real funds (default: testnet)
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import ccxt
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Load .env so API keys are available
load_dotenv(Path(__file__).parent / ".env")

console = Console()

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

ALLOCATION_USDT   = 5_000.0   # USDT deployed per trade
PROFIT_TARGET_PCT = 0.03      # +3% sell target
TIMEOUT_HOURS     = 1.0       # open sell-order timeout
DEPTH_PCT         = 0.02      # ±2% depth window around mid-price
SCAN_INTERVAL_S   = 3.0       # seconds between scan cycles
UPDATE_TICK_S     = 0.5       # how often to poll trade fills / refresh UI
MAX_LOG           = 26        # lines visible in event log
FAST              = "--fast"    in sys.argv
LIVE              = "--live"    in sys.argv
EXECUTE           = "--execute" in sys.argv and LIVE
SANDBOX           = "--sandbox" in sys.argv or os.getenv("EXCHANGE_SANDBOX", "true").lower() != "false"

# Simulated fill delays (real-clock seconds)
BUY_FILL_S  = (1.5, 4.0)  if FAST else (3.0,  9.0)
SELL_FILL_S = (8.0, 30.0) if FAST else (20.0, 90.0)

# ─────────────────────────────────────────────────────────────────────────────
# Candidate Universe
# All pass mock CoinMarketCap ≥ $5M 24h-vol gate.
# ─────────────────────────────────────────────────────────────────────────────

UNIVERSE: list[tuple[str, float]] = [
    ("BTC",  65_000.0), ("ETH",   3_200.0), ("SOL",    185.00),
    ("ARB",     1.230), ("AVAX",    38.50), ("LINK",    18.20),
    ("MATIC",   0.870), ("DOT",      9.20), ("ADA",     0.460),
    ("UNI",    12.100), ("OP",       2.900), ("APT",    11.50),
    ("INJ",    28.800), ("SUI",      2.100), ("NEAR",    7.60),
    ("WLD",     5.800), ("TIA",      9.100), ("JUP",     1.10),
    ("FTM",     0.920), ("LDO",      2.400),
]

# Pre-scheduled demo triggers: {scan_cycle: symbol}
# Ensures the screen stays active in demo without waiting for random events.
DEMO_TRIGGERS: dict[int, str] = {2: "ARB", 7: "SOL", 13: "LINK", 20: "OP", 28: "APT"}

# ─────────────────────────────────────────────────────────────────────────────
# Domain objects
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Level:
    price: float
    vol:   float


@dataclass
class OBook:
    symbol: str
    bids:   list[Level]
    asks:   list[Level]
    ts:     datetime = field(default_factory=datetime.utcnow)

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else float("inf")

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0


@dataclass
class DepthResult:
    buy_vol:  float   # sum of bid volume within −2% of mid
    sell_vol: float   # sum of ask volume within +2% of mid
    ratio:    float   # buy_vol / sell_vol
    passes:   bool    # True when buy > sell


@dataclass
class SimOrder:
    oid:      str
    pair:     str
    side:     str          # "BUY" | "SELL"
    qty:      float
    price:    float
    status:   str          # "OPEN" | "FILLED" | "TIMEOUT"
    created:  datetime
    fill_at:  datetime     # wall-clock time this order simulates as filled (mock) or sentinel (live)
    expires:  Optional[datetime] = None
    filled:   Optional[datetime] = None
    real_oid: Optional[str]      = None   # real exchange order ID when --execute


@dataclass
class SimTrade:
    tid:    str
    asset:  str
    alloc:  float
    buy:    SimOrder
    sell:   Optional[SimOrder] = None
    phase:  str = "BUYING"    # BUYING → HOLDING → DONE
    pnl:    Optional[float] = None
    closed: Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────────────────
# Mock Exchange
# ─────────────────────────────────────────────────────────────────────────────

class MockBybit:
    """
    Generates realistic mock order books with price drift.
    Injects deliberate spread + depth-imbalance events on scheduled cycles
    so the dashboard stays active during testing without random waiting.

    Drop-in compatible with ccxt.bybit for public order-book calls when
    real testnet credentials are provided (swap fetch_books() to use
    ccxt.bybit.fetch_order_book()).
    """

    def __init__(self) -> None:
        self._prices: dict[str, float] = {sym: px for sym, px in UNIVERSE}
        self._cycle = 0

    def _drift(self, sym: str) -> float:
        p = self._prices[sym] * (1.0 + random.gauss(0.0, 0.0006))
        self._prices[sym] = max(p, 0.0001)
        return self._prices[sym]

    def _build_book(
        self,
        symbol:        str,
        mid:           float,
        *,
        ask_bias:      float = 0.0,
        bid_bias:      float = 0.0,
        buy_imbalance: float = 1.0,   # multiplier for bid volumes vs ask volumes
        n_levels:      int   = 40,
    ) -> OBook:
        tick = mid * 0.0001
        bids: list[Level] = []
        asks: list[Level] = []
        for i in range(n_levels):
            decay = 0.87 ** i
            bids.append(Level(
                price=(mid + bid_bias) - tick * (i + 0.5) * (1.0 + random.uniform(0, 0.04)),
                vol  =random.uniform(150, 2_500) * buy_imbalance * decay,
            ))
            asks.append(Level(
                price=(mid + ask_bias) + tick * (i + 0.5) * (1.0 + random.uniform(0, 0.04)),
                vol  =random.uniform(150, 2_500) / buy_imbalance * decay,
            ))
        return OBook(symbol=symbol, bids=bids, asks=asks)

    def fetch_books(self, sym: str, *, trigger: bool = False) -> tuple[OBook, OBook]:
        """Return (USDT book, USDC book) for the given symbol."""
        mid = self._drift(sym)
        if trigger:
            # USDT pair: ask pushed up → creates positive spread
            usdt = self._build_book(f"{sym}/USDT", mid,
                                    ask_bias=+mid * 0.0018,
                                    buy_imbalance=1.65)
            # USDC pair: bid pulled slightly down
            usdc = self._build_book(f"{sym}/USDC", mid,
                                    bid_bias=-mid * 0.0007)
        else:
            # Normal: spread is flat or negative (no arb)
            noise_usdt = random.uniform(-0.0010, 0.0003)
            noise_usdc = random.uniform(-0.0010, 0.0001)
            usdt = self._build_book(f"{sym}/USDT", mid, ask_bias=mid * noise_usdt)
            usdc = self._build_book(f"{sym}/USDC", mid, bid_bias=mid * noise_usdc)
        return usdt, usdc

    def advance_cycle(self, cycle: int) -> Optional[str]:
        """Return the demo-trigger symbol for this cycle, if any."""
        self._cycle = cycle
        return DEMO_TRIGGERS.get(cycle)


# ─────────────────────────────────────────────────────────────────────────────
# Live Exchange — Real Bybit Order Books (public, no auth needed)
# ─────────────────────────────────────────────────────────────────────────────

class LiveBybit:
    """
    Fetches real order books from Bybit via CCXT public endpoints.
    No API key required. Drop-in replacement for MockBybit.

    At startup, discovers all assets listed in both X/USDT and X/USDC
    on Bybit Spot, filtered by ≥$1M 24h volume.
    """

    MIN_VOL_USD = 1_000_000.0   # 24h volume gate

    def __init__(self, *, sandbox: bool = True) -> None:
        self._ex = ccxt.bybit({
            "options":         {"defaultType": "spot"},
            "enableRateLimit": True,
            "sandbox":         sandbox,
        })
        console.print("[dim]Loading Bybit markets…[/]", end=" ")
        self._ex.load_markets()
        self._universe = self._discover()
        console.print(f"[green]OK[/]  — {len(self._universe)} dual-quote pairs")

    def _discover(self) -> list[tuple[str, float]]:
        """Return (base, mid_price) for every asset with both USDT and USDC spot markets."""
        pairs: list[tuple[str, float]] = []
        for sym, mkt in self._ex.markets.items():
            if not sym.endswith("/USDT"):
                continue
            base    = sym.split("/")[0]
            usdc_sym = f"{base}/USDC"
            if usdc_sym not in self._ex.markets:
                continue
            # Volume gate from market info (may be 0 if exchange doesn't supply it)
            vol = float(mkt.get("info", {}).get("volume24h", 0) or 0)
            mid = float(mkt.get("info", {}).get("lastPrice", 1) or 1)
            if vol >= self.MIN_VOL_USD or vol == 0:   # 0 means data not available, allow
                pairs.append((base, mid))
        # Fall back to UNIVERSE bases if discovery returns nothing useful
        if not pairs:
            pairs = UNIVERSE
        return pairs[:25]

    @property
    def universe(self) -> list[tuple[str, float]]:
        return self._universe

    def fetch_books(self, sym: str, *, trigger: bool = False) -> tuple[OBook, OBook]:
        """Fetch live order books; trigger flag is ignored (real markets)."""
        def _parse(raw: dict, symbol: str) -> OBook:
            bids = [Level(price=float(b[0]), vol=float(b[1])) for b in raw["bids"][:40]]
            asks = [Level(price=float(a[0]), vol=float(a[1])) for a in raw["asks"][:40]]
            return OBook(symbol=symbol, bids=bids, asks=asks)

        usdt_raw = self._ex.fetch_order_book(f"{sym}/USDT", limit=40)
        usdc_raw = self._ex.fetch_order_book(f"{sym}/USDC", limit=40)
        return _parse(usdt_raw, f"{sym}/USDT"), _parse(usdc_raw, f"{sym}/USDC")

    def advance_cycle(self, cycle: int) -> Optional[str]:
        return None   # no forced triggers; real markets generate their own signals


# ─────────────────────────────────────────────────────────────────────────────
# Live Executor — Real Order Placement (needs API key, --execute flag)
# ─────────────────────────────────────────────────────────────────────────────

class LiveBybitExecutor:
    """
    Places, monitors, and cancels real Bybit spot orders via CCXT.
    Requires EXCHANGE_API_KEY and EXCHANGE_SECRET in .env.
    Uses testnet by default (EXCHANGE_SANDBOX=false to disable).
    """

    def __init__(self, *, sandbox: bool = True) -> None:
        api_key = os.getenv("EXCHANGE_API_KEY", "")
        secret  = os.getenv("EXCHANGE_SECRET",  "")
        if not api_key or not secret:
            raise EnvironmentError(
                "EXCHANGE_API_KEY and EXCHANGE_SECRET must be set in .env to use --execute"
            )
        self._ex = ccxt.bybit({
            "apiKey":          api_key,
            "secret":          secret,
            "options":         {"defaultType": "spot"},
            "enableRateLimit": True,
            "sandbox":         sandbox,
        })
        self._ex.load_markets()

    def place_limit(self, symbol: str, side: str, qty: float, price: float) -> str:
        """Place a limit order. Returns the exchange order ID."""
        fn = self._ex.create_limit_buy_order if side == "BUY" else self._ex.create_limit_sell_order
        order = fn(symbol, qty, price)
        return str(order["id"])

    def cancel(self, order_id: str, symbol: str) -> None:
        try:
            self._ex.cancel_order(order_id, symbol)
        except Exception:
            pass   # already filled or expired

    def check(self, order_id: str, symbol: str) -> dict:
        """Return the ccxt order dict; status: 'open' | 'closed' | 'canceled'."""
        return self._ex.fetch_order(order_id, symbol)


# ─────────────────────────────────────────────────────────────────────────────
# Depth Analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyze_depth(book: OBook) -> DepthResult:
    """
    Sum bid volume within (mid − 2%) and ask volume within (mid + 2%).
    Strategy rule: proceed only if buy_vol > sell_vol.
    """
    mid      = book.mid
    lo       = mid * (1.0 - DEPTH_PCT)
    hi       = mid * (1.0 + DEPTH_PCT)
    buy_vol  = sum(lv.vol for lv in book.bids if lv.price >= lo)
    sell_vol = sum(lv.vol for lv in book.asks if lv.price <= hi)
    ratio    = buy_vol / sell_vol if sell_vol > 0 else float("inf")
    return DepthResult(buy_vol=buy_vol, sell_vol=sell_vol,
                       ratio=ratio, passes=ratio > 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Arbitrage Engine
# ─────────────────────────────────────────────────────────────────────────────

class Engine:

    def __init__(
        self,
        exchange: Optional[object] = None,   # MockBybit | LiveBybit
        executor: Optional[object] = None,   # None | LiveBybitExecutor
    ) -> None:
        self.ex       = exchange or MockBybit()
        self.executor = executor
        self.universe: list[tuple[str, float]] = getattr(self.ex, "universe", UNIVERSE)
        self.cycle  = 0
        self.start  = datetime.utcnow()
        self.active: list[SimTrade] = []
        self.done:   list[SimTrade] = []
        self.log:    list[tuple[str, str, str]] = []   # (hh:mm:ss, level, msg)
        self.stats = dict(
            scans=0, flags=0, depth_ok=0,
            trades=0, wins=0, timeouts=0, pnl=0.0,
        )

    # ── Logging ──────────────────────────────────────────────────────────────

    def _log(self, level: str, msg: str) -> None:
        ts = datetime.utcnow().strftime("%H:%M:%S")
        self.log.append((ts, level, msg))
        if len(self.log) > MAX_LOG + 10:
            self.log = self.log[-MAX_LOG:]

    # ── Scan ─────────────────────────────────────────────────────────────────

    async def scan(self) -> None:
        self.cycle += 1
        self.stats["scans"] += 1
        trigger_sym = self.ex.advance_cycle(self.cycle)
        self._log("CYCLE", f"Scan #{self.cycle} — polling {len(self.universe)} pairs")

        entered = False
        for sym, _ in self.universe:
            # Skip assets already in an active trade
            if any(t.asset == sym and t.phase != "DONE" for t in self.active):
                continue

            is_trig = (sym == trigger_sym and not entered)
            try:
                usdt_book, usdc_book = self.ex.fetch_books(sym, trigger=is_trig)
            except Exception as exc:
                self._log("CYCLE", f"[dim]  {sym}: fetch error — {exc}[/]")
                continue

            # ── Step 1: Spread Detection ──────────────────────────────────
            spread = usdt_book.best_ask - usdc_book.best_bid
            if spread <= 0.0:
                continue

            spread_pct = spread / usdc_book.best_bid * 100.0
            self.stats["flags"] += 1
            self._log("SPREAD",
                f"[yellow]★ {sym}[/]  ask(USDT) {usdt_book.best_ask:.5g}"
                f"  bid(USDC) {usdc_book.best_bid:.5g}"
                f"  Δ [green]+{spread_pct:.4f}%[/]")

            # ── Step 2: 2% Depth Imbalance Filter ────────────────────────
            d = analyze_depth(usdt_book)
            self.stats["depth_ok"] += int(d.passes)
            self._log("DEPTH",
                f"  {sym}  buy-vol {d.buy_vol:,.0f}"
                f"  {'>' if d.passes else '<'}"
                f"  sell-vol {d.sell_vol:,.0f}"
                f"  ({d.ratio:.2f}×)"
                f"  {'[green]✓ PASS[/]' if d.passes else '[red]✗ FAIL[/]'}")

            if not d.passes:
                continue

            # ── Step 3: Simulate Execution ────────────────────────────────
            await self._enter(sym, usdc_bid=usdc_book.best_bid)
            entered = True
            break   # one entry per scan cycle

    # ── Entry Simulation ─────────────────────────────────────────────────────

    async def _enter(self, sym: str, usdc_bid: float) -> None:
        now   = datetime.utcnow()
        qty   = round(ALLOCATION_USDT / usdc_bid, 4)

        self._log("EXEC",
            f"  {'Placing real' if self.executor else 'Converting'}"
            f" [yellow]{ALLOCATION_USDT:,.0f} USDT[/] → USDC"
            f"  ({'live order' if self.executor else '1:1 sim'})")

        real_oid: Optional[str] = None
        if self.executor:
            try:
                real_oid = self.executor.place_limit(f"{sym}/USDC", "BUY", qty, usdc_bid)
            except Exception as exc:
                self._log("EXEC", f"  [red]Order placement failed:[/] {exc}")
                return
            delay    = 0.0   # poll real status instead of simulated fill time
            fill_at  = now + timedelta(hours=24)   # sentinel; real status checked first
        else:
            delay   = random.uniform(*BUY_FILL_S)
            fill_at = now + timedelta(seconds=delay)

        buy = SimOrder(
            oid      = real_oid or uuid.uuid4().hex[:8],
            pair     = f"{sym}/USDC",
            side     = "BUY",
            qty      = qty,
            price    = usdc_bid,
            status   = "OPEN",
            created  = now,
            fill_at  = fill_at,
            real_oid = real_oid,
        )
        fill_hint = f"  (order {real_oid})" if real_oid else f"  (~{delay:.0f}s fill)"
        self._log("ORDER",
            f"  [cyan]► BUY LIMIT[/]  {sym}/USDC"
            f"  {qty:,.4f} @ {usdc_bid:.6f} USDC{fill_hint}")

        trade = SimTrade(
            tid   = uuid.uuid4().hex[:6].upper(),
            asset = sym,
            alloc = ALLOCATION_USDT,
            buy   = buy,
        )
        self.active.append(trade)
        self.stats["trades"] += 1
        self._log("TRACK", f"  Trade [[bold]{trade.tid}[/]] opened — awaiting BUY fill")

    # ── Trade Lifecycle Updates ───────────────────────────────────────────────

    def _buy_is_filled(self, t: "SimTrade", now: datetime) -> bool:
        """True when the buy order should be treated as filled."""
        if self.executor and t.buy.real_oid:
            try:
                info = self.executor.check(t.buy.real_oid, t.buy.pair)
                if info.get("status") == "closed":
                    # Use actual fill price if available
                    t.buy.price = float(info.get("average") or t.buy.price)
                    return True
                return False
            except Exception:
                return False
        return now >= t.buy.fill_at

    def _sell_is_filled(self, t: "SimTrade", now: datetime) -> bool:
        """True when the sell order should be treated as filled."""
        sl = t.sell
        if sl is None:
            return False
        if self.executor and sl.real_oid:
            try:
                info = self.executor.check(sl.real_oid, sl.pair)
                if info.get("status") == "closed":
                    sl.price = float(info.get("average") or sl.price)
                    return True
                return False
            except Exception:
                return False
        return now >= sl.fill_at

    async def update(self) -> None:
        now  = datetime.utcnow()
        done = []

        for t in self.active:
            if t.phase == "BUYING":
                if self._buy_is_filled(t, now):
                    t.buy.status = "FILLED"
                    t.buy.filled = now
                    t.phase      = "HOLDING"

                    # (c) Calculate sell price at +3%
                    sell_px = round(t.buy.price * (1.0 + PROFIT_TARGET_PCT), 6)
                    expires = now + timedelta(hours=TIMEOUT_HOURS)

                    real_sell_oid: Optional[str] = None
                    if self.executor:
                        try:
                            real_sell_oid = self.executor.place_limit(
                                f"{t.asset}/USDT", "SELL", t.buy.qty, sell_px
                            )
                        except Exception as exc:
                            self._log("EXEC", f"  [red]SELL order failed:[/] {exc}")

                    sell_delay = 0.0 if self.executor else random.uniform(*SELL_FILL_S)
                    sell = SimOrder(
                        oid      = real_sell_oid or uuid.uuid4().hex[:8],
                        pair     = f"{t.asset}/USDT",
                        side     = "SELL",
                        qty      = t.buy.qty,
                        price    = sell_px,
                        status   = "OPEN",
                        created  = now,
                        fill_at  = now + timedelta(seconds=sell_delay) if not self.executor
                                   else now + timedelta(hours=24),
                        expires  = expires,
                        real_oid = real_sell_oid,
                    )
                    t.sell = sell

                    self._log("FILL",
                        f"  [green]✓ BUY FILLED[/]  [[bold]{t.tid}[/]]"
                        f"  {t.buy.pair}  {t.buy.qty:,.4f} @ {t.buy.price:.6f}")

                    hint = f"  (order {real_sell_oid})" if real_sell_oid else \
                           f"  (~{sell_delay:.0f}s fill)"
                    self._log("ORDER",
                        f"  [yellow]► SELL LIMIT[/]  {t.asset}/USDT"
                        f"  {sell.qty:,.4f} @ {sell_px:.6f}"
                        f"  ([green]+{PROFIT_TARGET_PCT*100:.0f}%[/])"
                        f"  exp {TIMEOUT_HOURS:.0f}h{hint}")

            elif t.phase == "HOLDING" and t.sell is not None:
                sl = t.sell
                # Timeout check
                if sl.expires and now >= sl.expires:
                    if self.executor and sl.real_oid:
                        self.executor.cancel(sl.real_oid, sl.pair)
                    sl.status       = "TIMEOUT"
                    t.phase         = "DONE"
                    t.pnl           = 0.0
                    t.closed        = now
                    self.stats["timeouts"] += 1
                    self._log("TIMEOUT",
                        f"  [red]✗ TIMEOUT[/]  [[bold]{t.tid}[/]]  {sl.pair}"
                        f"  — order expired after {TIMEOUT_HOURS:.0f}h")
                    done.append(t)

                # Fill check (simulated timestamp OR real exchange poll)
                elif self._sell_is_filled(t, now):
                    sl.status  = "FILLED"
                    sl.filled  = now
                    t.phase    = "DONE"
                    gross      = (sl.price - t.buy.price) * sl.qty
                    net        = gross * (1.0 - 0.0006)  # ~0.03% commission × 2 legs
                    t.pnl      = net
                    t.closed   = now
                    self.stats["wins"] += 1
                    self.stats["pnl"]  += net
                    c = "green" if net >= 0 else "red"
                    self._log("FILL",
                        f"  [bold green]✓ SELL FILLED[/]  [[bold]{t.tid}[/]]"
                        f"  {sl.pair}  {sl.qty:,.4f} @ {sl.price:.6f}"
                        f"  PnL [{c}]+${net:,.2f}[/{c}]")
                    done.append(t)

        for t in done:
            self.active.remove(t)
            self.done.append(t)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard Renderer
# ─────────────────────────────────────────────────────────────────────────────

_LEVEL_STYLE: dict[str, str] = {
    "CYCLE":   "dim",
    "SPREAD":  "yellow",
    "DEPTH":   "cyan",
    "EXEC":    "yellow",
    "ORDER":   "blue",
    "FILL":    "green",
    "TRACK":   "dim cyan",
    "TIMEOUT": "red",
}


def _runtime(start: datetime) -> str:
    s      = int((datetime.utcnow() - start).total_seconds())
    h, r   = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def render(eng: Engine) -> Layout:
    now = datetime.utcnow()
    st  = eng.stats
    pnl_col = "green" if st["pnl"] >= 0 else "red"
    wr      = f"{st['wins']/st['trades']*100:.0f}%" if st["trades"] else "—"

    # ── Header bar ───────────────────────────────────────────────────────────
    if EXECUTE:
        mode_str = "[bold red]LIVE + EXECUTE[/]"
    elif LIVE:
        mode_str = f"[bold green]LIVE{'  TESTNET' if SANDBOX else '  MAINNET'}[/]"
    else:
        mode_str = f"[yellow]MOCK{'  FAST' if FAST else ''}[/]"

    header_markup = (
        f"[bold cyan]◈  USDT/USDC CROSS-PAIR ARB SCANNER[/]"
        f"  [dim]│[/]  Bybit Spot"
        f"  [dim]│[/]  Cycle [cyan]#{eng.cycle}[/]"
        f"  [dim]│[/]  Runtime [cyan]{_runtime(eng.start)}[/]"
        f"  [dim]│[/]  Trades [cyan]{st['trades']}[/]"
        f"  [dim]│[/]  Win Rate [cyan]{wr}[/]"
        f"  [dim]│[/]  PnL [{pnl_col}]{st['pnl']:+,.2f} USDT[/{pnl_col}]"
        f"  [dim]│[/]  {mode_str}"
    )
    header_panel = Panel(
        Text.from_markup(header_markup),
        border_style="cyan",
        padding=(0, 1),
    )

    # ── Stats panel ──────────────────────────────────────────────────────────
    stat_tbl = Table(box=None, show_header=False, padding=(0, 1))
    stat_tbl.add_column(style="dim", width=16)
    stat_tbl.add_column(style="bold cyan", justify="right", width=8)

    stat_rows = [
        ("Scan Cycles",  str(st["scans"])),
        ("Spread Hits",  str(st["flags"])),
        ("Depth Passes", str(st["depth_ok"])),
        ("──────────",   "────────"),
        ("Trades",       str(st["trades"])),
        ("Wins",         str(st["wins"])),
        ("Timeouts",     str(st["timeouts"])),
        ("──────────",   "────────"),
        ("Deployed $",   f"{sum(t.alloc for t in eng.active):,.0f}"),
    ]
    for label, val in stat_rows:
        stat_tbl.add_row(label, val)

    stat_tbl.add_row(
        Text("Total PnL", style="dim"),
        Text(f"{st['pnl']:+,.2f}", style=pnl_col),
    )

    stat_panel = Panel(
        stat_tbl,
        title="[dim]Session Stats[/dim]",
        border_style="dim cyan",
        width=30,
        padding=(0, 1),
    )

    # ── Event log ────────────────────────────────────────────────────────────
    log_tbl = Table(box=None, show_header=False, padding=(0, 0), expand=True)
    log_tbl.add_column(style="dim",     width=9,  no_wrap=True)
    log_tbl.add_column(width=7,                   no_wrap=True)
    log_tbl.add_column(overflow="fold")

    for ts, lvl, msg in eng.log[-MAX_LOG:]:
        sty = _LEVEL_STYLE.get(lvl, "white")
        log_tbl.add_row(
            Text(ts,    style="dim"),
            Text(lvl[:6], style=sty),
            Text.from_markup(msg) if "[" in msg else Text(msg, style=sty),
        )

    log_panel = Panel(
        log_tbl,
        title="[dim]Event Log[/dim]",
        border_style="dim white",
        expand=True,
        padding=(0, 1),
    )

    # ── Orders table ─────────────────────────────────────────────────────────
    ord_tbl = Table(box=box.SIMPLE_HEAD, expand=True)
    ord_tbl.add_column("Trade ID", style="dim bold", width=9)
    ord_tbl.add_column("Pair",     style="bold",     width=13)
    ord_tbl.add_column("Side",                       width=9)
    ord_tbl.add_column("Quantity", justify="right",  width=14)
    ord_tbl.add_column("Price",    justify="right",  width=14)
    ord_tbl.add_column("Status",                     width=13)
    ord_tbl.add_column("Timeout",  justify="right",  width=14)

    visible = eng.active + eng.done[-5:]
    if not visible:
        ord_tbl.add_row(
            "—", "Awaiting signal…", "—", "—", "—",
            Text("Scanning", style="dim"), "—",
        )

    for tr in visible:
        orders_to_show = [tr.buy] + ([tr.sell] if tr.sell else [])
        for ord_ in orders_to_show:
            side_t = (
                Text("▼ BUY",  style="bold green")
                if ord_.side == "BUY"
                else Text("▲ SELL", style="bold red")
            )

            if ord_.status == "FILLED":
                st_t = Text("● FILLED",  style="bold green")
            elif ord_.status == "TIMEOUT":
                st_t = Text("✗ TIMEOUT", style="red dim")
            elif tr.phase == "BUYING" and ord_.side == "BUY":
                st_t = Text("◌ FILLING…", style="yellow")
            else:
                st_t = Text("◎ OPEN",    style="cyan")

            if ord_.expires and ord_.status == "OPEN":
                rem = int((ord_.expires - now).total_seconds())
                tl  = f"{rem // 60}m {rem % 60:02d}s" if rem > 0 else "expired"
            else:
                tl = "—"

            pnl_suffix = ""
            if ord_.side == "SELL" and tr.pnl is not None and ord_.status == "FILLED":
                c = "green" if tr.pnl >= 0 else "red"
                pnl_suffix = f"  [{c}]+${tr.pnl:,.2f}[/{c}]"

            ord_tbl.add_row(
                tr.tid,
                ord_.pair,
                side_t,
                f"{ord_.qty:,.4f}",
                f"{ord_.price:.6f}",
                st_t,
                Text.from_markup(tl + pnl_suffix) if pnl_suffix else Text(tl),
            )

    orders_panel = Panel(
        ord_tbl,
        title="[bold white]Active & Recent Orders[/bold white]",
        border_style="blue",
        padding=(0, 1),
    )

    # ── Assemble layout ───────────────────────────────────────────────────────
    layout = Layout()
    layout.split_column(
        Layout(header_panel,  name="header",  size=3),
        Layout(name="body"),
        Layout(orders_panel,  name="orders",  size=12),
    )
    layout["body"].split_row(
        Layout(stat_panel,   name="stats",  minimum_size=30),
        Layout(log_panel,    name="log"),
    )
    return layout


# ─────────────────────────────────────────────────────────────────────────────
# Exchange / executor factory
# ─────────────────────────────────────────────────────────────────────────────

def build_exchange() -> tuple[object, Optional[object]]:
    """Return (exchange, executor) based on CLI flags."""
    if not LIVE:
        return MockBybit(), None

    exchange = LiveBybit(sandbox=SANDBOX)

    if not EXECUTE:
        return exchange, None

    try:
        executor = LiveBybitExecutor(sandbox=SANDBOX)
        console.print("[green]Executor ready[/] — real orders will be placed on Bybit"
                      + (" [yellow](TESTNET)[/]" if SANDBOX else " [bold red](MAINNET — REAL FUNDS)[/]"))
        return exchange, executor
    except EnvironmentError as exc:
        console.print(f"[red]Cannot enable --execute:[/] {exc}")
        raise SystemExit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def run() -> None:
    # ── Build exchange / executor ─────────────────────────────────────────────
    exchange, executor = build_exchange()
    n_pairs = len(getattr(exchange, "universe", UNIVERSE))

    if EXECUTE:
        mode_label = "LIVE + EXECUTE  (" + ("TESTNET" if SANDBOX else "MAINNET — REAL FUNDS") + ")"
        mode_color = "red"
    elif LIVE:
        mode_label = "LIVE  (" + ("TESTNET" if SANDBOX else "MAINNET") + ")"
        mode_color = "green"
    else:
        mode_label = "MOCK" + ("  FAST" if FAST else "")
        mode_color = "yellow"

    console.print(Panel(
        f"  [bold cyan]USDT/USDC Cross-Pair Arbitrage Simulator[/bold cyan]\n\n"
        f"  [dim]Exchange  :[/dim] Bybit Spot\n"
        f"  [dim]Pairs     :[/dim] {n_pairs} candidates (dual USDT+USDC)\n"
        f"  [dim]Allocation:[/dim] [yellow]{ALLOCATION_USDT:,.0f} USDT[/] per trade\n"
        f"  [dim]Target    :[/dim] [green]+{PROFIT_TARGET_PCT*100:.0f}%[/] sell above entry\n"
        f"  [dim]Depth gate:[/dim] ±{DEPTH_PCT*100:.0f}% buy/sell imbalance\n"
        f"  [dim]Timeout   :[/dim] {TIMEOUT_HOURS:.0f}h per open sell order\n"
        f"  [dim]Mode      :[/dim] [{mode_color}]{mode_label}[/{mode_color}]\n\n"
        f"  [dim]Ctrl+C to stop[/dim]",
        title="[bold cyan]▶  Starting[/bold cyan]",
        border_style="cyan",
    ))
    console.print()
    await asyncio.sleep(0.5 if LIVE else 1.5)

    eng       = Engine(exchange=exchange, executor=executor)
    last_scan = 0.0

    try:
        use_screen = sys.stdout.isatty() and "--no-screen" not in sys.argv
        with Live(render(eng), console=console, screen=use_screen,
                  refresh_per_second=4) as live:
            while True:
                now_m = time.monotonic()
                if now_m - last_scan >= SCAN_INTERVAL_S:
                    await eng.scan()
                    last_scan = now_m

                await eng.update()
                live.update(render(eng))
                await asyncio.sleep(UPDATE_TICK_S)

    except KeyboardInterrupt:
        pass

    # ── Final report ─────────────────────────────────────────────────────────
    st = eng.stats
    console.print()
    console.print(Panel(
        f"[bold]Final Session Report[/bold]\n\n"
        f"  Scan Cycles   : {st['scans']}\n"
        f"  Spread Hits   : {st['flags']}\n"
        f"  Depth Passes  : {st['depth_ok']}\n"
        f"  Trades Entered: {st['trades']}\n"
        f"  Wins (filled) : {st['wins']}\n"
        f"  Timeouts      : {st['timeouts']}\n"
        f"  Pending       : {len(eng.active)}\n"
        f"  Total PnL     : [{'green' if st['pnl'] >= 0 else 'red'}]{st['pnl']:+,.2f} USDT[/]",
        title="[bold cyan]Simulation Complete[/bold cyan]",
        border_style="cyan",
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Test / headless mode  (--test N)
# Runs N scan cycles without a Live dashboard — useful for CI and capture.
# ─────────────────────────────────────────────────────────────────────────────

_LEVEL_ICONS = {
    "CYCLE": "[dim]──[/]",
    "SPREAD": "[yellow]★[/]",
    "DEPTH": "[cyan]⊞[/]",
    "EXEC": "[yellow]⇄[/]",
    "ORDER": "[blue]►[/]",
    "FILL": "[green]✓[/]",
    "TRACK": "[dim cyan]…[/]",
    "TIMEOUT": "[red]✗[/]",
}


async def run_test(cycles: int = 30) -> None:
    """Headless test: run `cycles` scan cycles, printing each event live."""
    exchange, executor = build_exchange() if LIVE else (None, None)
    console.print(Panel(
        f"[bold cyan]USDT/USDC Arb Simulator — HEADLESS TEST MODE[/bold cyan]\n"
        f"[dim]Running {cycles} scan cycles. No Live dashboard.[/dim]\n"
        f"[dim]Mode: {'LIVE' if LIVE else 'MOCK'}{'  EXECUTE' if EXECUTE else ''}[/dim]",
        border_style="cyan",
    ))

    eng    = Engine(exchange=exchange, executor=executor)
    seen   = 0  # log lines already printed

    def flush_log() -> None:
        nonlocal seen
        for ts, lvl, msg in eng.log[seen:]:
            icon = _LEVEL_ICONS.get(lvl, "")
            line = Text.from_markup(f"[dim]{ts}[/]  {icon}  {msg}")
            console.print(line)
        seen = len(eng.log)

    for _ in range(cycles):
        await eng.scan()
        flush_log()
        # Pump update several times per scan to catch fills
        for _ in range(6):
            await eng.update()
            flush_log()
            await asyncio.sleep(0.5)

    # Flush any remaining
    flush_log()

    # Final report
    st = eng.stats
    console.print()
    console.print(Panel(
        f"[bold]Test Run Summary — {cycles} scan cycles[/bold]\n\n"
        f"  Spread Hits   : {st['flags']}\n"
        f"  Depth Passes  : {st['depth_ok']}\n"
        f"  Trades Entered: {st['trades']}\n"
        f"  Wins (filled) : {st['wins']}\n"
        f"  Timeouts      : {st['timeouts']}\n"
        f"  Pending open  : {len(eng.active)}\n"
        f"  Total PnL     : [{'green' if st['pnl'] >= 0 else 'red'}]{st['pnl']:+,.2f} USDT[/]",
        title="[bold cyan]Test Complete[/bold cyan]",
        border_style="cyan",
    ))


if __name__ == "__main__":
    if "--test" in sys.argv:
        try:
            idx    = sys.argv.index("--test")
            cycles = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 30
        except (ValueError, IndexError):
            cycles = 30
        asyncio.run(run_test(cycles))
    else:
        asyncio.run(run())
