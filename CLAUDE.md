# tradetest01 — Claude Code Project Guide

## What this is

A quantitative trading toolkit with three layers:

1. **Transcript-to-Strategy CLI** (`main.py`) — parse trading strategy videos/transcripts with Claude AI, extract logic, generate executable Backtrader/CCXT code, and run backtests or paper trades.
2. **Arbitrage Simulator** (`arbitrage_sim.py`) — live USDT/USDC cross-pair spread scanner with real Bybit order book integration and optional real order execution.
3. **Historical Backtest** (`backtest_arb.py`) — bar-by-bar backtest engine using real OHLCV from Bybit (with GBM+OU synthetic fallback).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in your keys
```

`.env` keys:
```
ANTHROPIC_API_KEY=sk-ant-...       # required for `parse` command
EXCHANGE_API_KEY=...               # required for --execute mode
EXCHANGE_SECRET=...                # required for --execute mode
EXCHANGE_SANDBOX=true              # false = real funds
```

## Run commands

### Full pipeline demo (no API key needed)
```bash
python main.py demo
```

### Parse a transcript → extract strategy blueprint
```bash
python main.py parse transcripts/my_video.txt
python main.py parse transcripts/my_video.txt --name my_strategy
```

### Review extracted blueprint
```bash
python main.py review my_strategy
python main.py review my_strategy --raw     # raw JSON
```

### Generate trading code
```bash
python main.py compile my_strategy                  # Backtrader (default)
python main.py compile my_strategy --mode ccxt      # CCXT / live trading
```

### Backtest
```bash
python main.py backtest my_strategy
python main.py backtest my_strategy --cash 50000 --data path/to/data.csv
```

### Paper trade (live dashboard, synthetic data)
```bash
python main.py deploy my_strategy
```

### List all strategies
```bash
python main.py list
```

### Interactive dashboard
```bash
python main.py dashboard
```

---

### Arbitrage scanner
```bash
python arbitrage_sim.py                      # mock mode (no internet needed)
python arbitrage_sim.py --fast               # mock + accelerated fills
python arbitrage_sim.py --live               # real Bybit order books
python arbitrage_sim.py --live --execute     # real order books + real orders
python arbitrage_sim.py --test 20            # headless 20-cycle test
python arbitrage_sim.py --no-screen          # disable Rich Live (pipes/CI)
```

### Historical backtest (USDT/USDC arb strategy)
```bash
python backtest_arb.py                       # tries Bybit API, falls back to synthetic
```

---

## Project layout

```
main.py                  — Typer CLI entry point
arbitrage_sim.py         — Arb scanner (standalone)
backtest_arb.py          — Historical backtest (standalone)
config.py                — Env/path configuration
requirements.txt

db/
  __init__.py            — SQLite helpers (save/get/list blueprints)

parser/
  schema.py              — Pydantic StrategyBlueprint model
  extractor.py           — Claude API call → structured blueprint

strategies/
  generator.py           — Blueprint → Backtrader or CCXT Python code
  output/                — Generated strategy files (gitignored)

execution/
  paper_trader.py        — MockExchange + CCXT paper trading loop
  dashboard.py           — Rich interactive menu

transcripts/             — Drop .txt files here for parsing
```

## Key data model

```python
StrategyBlueprint(
    name, asset_class, timeframe, symbols,
    indicators,           # [{"name": "EMA", "params": {"period": 20}}]
    long_conditions,      # ["EMA_20 crosses above EMA_50", "RSI(14) < 70"]
    short_conditions,
    exit_long_conditions,
    exit_short_conditions,
    stop_loss,            # {"type": "percent", "value": 2.0}
    take_profit,          # {"type": "risk_reward", "ratio": 2.0}
    position_sizing,      # {"type": "fixed_percent", "value": 5.0}
)
```

## Arbitrage strategy parameters

Defined at top of `arbitrage_sim.py`:

| Constant | Default | Meaning |
|---|---|---|
| `ALLOCATION_USDT` | 5000 | USDT per trade |
| `PROFIT_TARGET_PCT` | 0.03 | +3% sell target |
| `TIMEOUT_HOURS` | 1.0 | Max time holding sell order |
| `DEPTH_PCT` | 0.02 | ±2% depth analysis window |
| `SCAN_INTERVAL_S` | 3.0 | Seconds between scans |

## Backtest parameters

Defined at top of `backtest_arb.py`:

| Constant | Default | Meaning |
|---|---|---|
| `TARGET_PCT` | 0.03 | +3% take-profit |
| `STOP_PCT` | 0.01 | -1% stop-loss |
| `TIMEOUT_BARS` | 24 | Max bars held (1h bars → 24h) |
| `ALLOCATION` | 5000 | USDT per trade |
| `DAYS` | 90 | Lookback period |
| `ASSETS` | 10 pairs | BTC ETH SOL ARB AVAX LINK MATIC DOT ADA UNI |

## Last backtest results (synthetic, 90d)

747 trades · 36.0% win rate · R:R 2.1:1 · Net PnL +$4,831 · Sharpe 6.68
Best: ETH (50.4% WR), UNI (41.4%), ADA (39.4%)
Weak: DOT (13%), AVAX (22%), SOL (30%)

## Generated code location

Compiled strategies go to `strategies/output/` (gitignored):
- `strategies/output/{name}_backtrader.py`
- `strategies/output/{name}_ccxt.py`

## Database

SQLite at `db/strategies.db` (gitignored). Schema:
- `strategies` — name, status (parsed/compiled/deployed), blueprint JSON, source file, timestamps
- `deployments` — strategy, mode, pnl, trades, log, timestamp

## Claude model

`claude-sonnet-4-6` via `ANTHROPIC_API_KEY`. Configured in `config.py` as `CLAUDE_MODEL`.

## Python version

3.11+ required (uses `match`, `Self`, walrus operator in places).
