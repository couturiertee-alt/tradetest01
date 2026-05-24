"""
tradetest01 — Transcript-to-Strategy CLI
Usage:  python main.py [COMMAND] [OPTIONS]
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

app     = typer.Typer(name="tradetest", add_completion=False,
                      help="Ingest transcripts → extract strategy → generate & run trading code.")
console = Console()


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

@app.command()
def parse(
    transcript: str = typer.Argument(..., help="Path or filename inside /transcripts"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Override strategy name"),
):
    """Parse a transcript file and extract a strategy blueprint via Claude."""
    from config import TRANSCRIPTS_DIR
    import db
    from parser.extractor import parse_transcript

    path = Path(transcript)
    if not path.is_absolute():
        path = TRANSCRIPTS_DIR / transcript

    with console.status(f"[cyan]Parsing {path.name}...[/cyan]"):
        try:
            blueprint = parse_transcript(path, strategy_name=name)
        except (FileNotFoundError, ValueError, EnvironmentError) as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    db.save_blueprint(blueprint.name, blueprint.model_dump(), source_file=path.name)

    console.print(Panel(
        f"[bold green]Strategy extracted:[/bold green] {blueprint.name}\n"
        f"Asset class : {blueprint.asset_class}   Timeframe: {blueprint.timeframe}\n"
        f"Indicators  : {', '.join(i.name for i in blueprint.indicators) or 'none'}\n"
        f"Long conds  : {len(blueprint.long_conditions)}\n"
        f"Short conds : {len(blueprint.short_conditions)}\n"
        f"Stop-loss   : {blueprint.stop_loss}\n"
        f"Take-profit : {blueprint.take_profit}",
        title="[bold cyan]Blueprint Saved[/bold cyan]",
        border_style="green",
    ))
    console.print(f"\n[dim]Run:[/dim]  python main.py review {blueprint.name}")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@app.command()
def review(
    name: str = typer.Argument(..., help="Strategy name"),
    raw:  bool = typer.Option(False,  "--raw",  help="Print raw JSON"),
):
    """Display the extracted strategy blueprint in a rich table."""
    import db

    data = db.get_blueprint(name)
    if data is None:
        console.print(f"[red]Strategy '{name}' not found. Run `parse` first.[/red]")
        raise typer.Exit(1)

    if raw:
        console.print_json(json.dumps(data, indent=2))
        return

    from parser.schema import StrategyBlueprint
    bp = StrategyBlueprint.model_validate(data)

    console.print(Panel(
        f"[bold]{bp.name}[/bold]\n"
        f"Asset class  : {bp.asset_class}\n"
        f"Timeframe    : {bp.timeframe}\n"
        f"Symbols      : {', '.join(bp.symbols)}\n"
        f"Notes        : {bp.notes or '—'}",
        title="[bold cyan]Strategy Blueprint[/bold cyan]",
        border_style="cyan",
    ))

    # Indicators table
    if bp.indicators:
        tbl = Table(title="Indicators", border_style="dim")
        tbl.add_column("Name",   style="bold yellow")
        tbl.add_column("Params", style="dim")
        for ind in bp.indicators:
            tbl.add_row(ind.name, str(ind.params) if ind.params else "defaults")
        console.print(tbl)

    # Conditions
    def cond_table(title: str, conds: list[str], color: str) -> None:
        if not conds:
            return
        tbl = Table(title=title, border_style=color)
        tbl.add_column("#",   style="dim",   width=3)
        tbl.add_column("Condition", style=color)
        for i, c in enumerate(conds, 1):
            tbl.add_row(str(i), c)
        console.print(tbl)

    cond_table("Long Entry Conditions",  bp.long_conditions,       "green")
    cond_table("Short Entry Conditions", bp.short_conditions,      "red")
    cond_table("Exit Long Conditions",   bp.exit_long_conditions,  "yellow")
    cond_table("Exit Short Conditions",  bp.exit_short_conditions, "magenta")

    # Risk management
    risk_tbl = Table(title="Risk Management", border_style="blue")
    risk_tbl.add_column("Setting",  style="bold")
    risk_tbl.add_column("Value")
    if bp.stop_loss:
        risk_tbl.add_row("Stop Loss", f"{bp.stop_loss.type}  value={bp.stop_loss.value}  mult={bp.stop_loss.multiplier}")
    if bp.take_profit:
        risk_tbl.add_row("Take Profit", f"{bp.take_profit.type}  value={bp.take_profit.value}  ratio={bp.take_profit.ratio}")
    risk_tbl.add_row("Position Size", f"{bp.position_sizing.type}  {bp.position_sizing.value}%")
    console.print(risk_tbl)


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------

@app.command()
def compile(
    name: str  = typer.Argument(..., help="Strategy name"),
    mode: str  = typer.Option("backtrader", "--mode", "-m",
                               help="Output mode: backtrader | ccxt"),
):
    """Generate executable Python strategy code from a blueprint."""
    import db
    from strategies.generator import compile_strategy

    with console.status(f"[cyan]Compiling {name} ({mode})...[/cyan]"):
        try:
            out = compile_strategy(name, mode=mode)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    db.update_status(name, "compiled")
    console.print(Panel(
        f"[bold green]Strategy compiled![/bold green]\n"
        f"Output  : {out}\n"
        f"Mode    : {mode}",
        title="[bold cyan]Compile Complete[/bold cyan]",
        border_style="green",
    ))
    if mode == "backtrader":
        console.print(f"\n[dim]Backtest it:[/dim]  python main.py backtest {name}")
    else:
        console.print(f"\n[dim]Paper trade:[/dim]  python main.py deploy {name}")


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

@app.command()
def backtest(
    name:      str            = typer.Argument(..., help="Strategy name"),
    data_path: Optional[str]  = typer.Option(None,  "--data",  "-d",  help="CSV data file"),
    cash:      float          = typer.Option(100_000.0, "--cash",  "-c",  help="Starting capital"),
    plot:      bool           = typer.Option(False, "--plot",  "-p",  help="Show plot"),
):
    """Run a Backtrader backtest on a compiled strategy."""
    from config import STRATEGIES_DIR
    import importlib.util, sys

    strategy_file = STRATEGIES_DIR / f"{name}_backtrader.py"
    if not strategy_file.exists():
        console.print(f"[red]No compiled backtrader strategy at {strategy_file}[/red]")
        console.print(f"[yellow]Run: python main.py compile {name}[/yellow]")
        raise typer.Exit(1)

    spec   = importlib.util.spec_from_file_location(f"bt_{name}", strategy_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"bt_{name}"] = module
    spec.loader.exec_module(module)

    console.print(Panel(f"[bold cyan]Backtesting:[/bold cyan] {name}  cash={cash:,.0f}",
                        border_style="cyan"))
    results = module.run_backtest(data_path=data_path, cash=cash, plot=plot)


# ---------------------------------------------------------------------------
# deploy (paper trade)
# ---------------------------------------------------------------------------

@app.command()
def deploy(
    name:  str   = typer.Argument(..., help="Strategy name"),
    speed: float = typer.Option(0.05, "--speed", "-s",
                                 help="Seconds between ticks in mock mode (0=max speed)"),
):
    """Paper-trade a compiled strategy against synthetic market data."""
    import db
    from execution.paper_trader import run_paper_trade

    result = run_paper_trade(name, speed=speed)
    if result:
        db.log_deployment(
            strategy=name,
            mode="paper",
            pnl=result.get("pnl", 0),
            trades=result.get("trades", 0),
            log=result.get("log", ""),
        )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@app.command(name="list")
def list_cmd():
    """List all strategies and their current status."""
    import db
    rows = db.list_strategies()
    if not rows:
        console.print("[yellow]No strategies found. Run `parse` to get started.[/yellow]")
        return

    tbl = Table(title="Strategies", border_style="cyan")
    tbl.add_column("Name",     style="bold")
    tbl.add_column("Status",   style="cyan")
    tbl.add_column("Source",   style="dim")
    tbl.add_column("Updated",  style="dim")

    status_colors = {"parsed": "yellow", "compiled": "cyan", "deployed": "green"}
    for r in rows:
        c = status_colors.get(r["status"], "white")
        tbl.add_row(
            r["name"],
            Text(r["status"], style=c),
            r.get("source_file") or "—",
            r["updated_at"][:16],
        )
    console.print(tbl)


# ---------------------------------------------------------------------------
# dashboard (interactive)
# ---------------------------------------------------------------------------

@app.command()
def dashboard():
    """Launch the interactive terminal dashboard."""
    from execution.dashboard import run_dashboard
    run_dashboard()


# ---------------------------------------------------------------------------
# demo — create a sample transcript and run the full pipeline offline
# ---------------------------------------------------------------------------

@app.command()
def demo():
    """Create a sample transcript and run parse→compile→backtest without an API key."""
    from config import TRANSCRIPTS_DIR, STRATEGIES_DIR
    import db

    sample_transcript = TRANSCRIPTS_DIR / "sample_ema_crossover.txt"
    sample_transcript.write_text(
        "In this video I'll explain my EMA crossover strategy for Bitcoin on the 1-hour chart.\n\n"
        "I use a 20-period EMA and a 50-period EMA. When the fast EMA crosses above the slow EMA "
        "I go long. When the fast EMA crosses below the slow EMA I exit or go short.\n\n"
        "For risk management: stop loss is 2% below entry. Take profit target is 2:1 risk/reward ratio.\n"
        "I size my positions at 5% of total account equity per trade.\n\n"
        "This works best on BTC/USDT. I only trade in the direction of the daily trend.\n"
        "I also use RSI(14) to filter out overbought entries — I won't long if RSI is above 70.\n",
        encoding="utf-8",
    )
    console.print(f"[green]Sample transcript written:[/green] {sample_transcript}")

    # Inject blueprint directly (no API key needed)
    blueprint_data = {
        "name": "ema_crossover_btc",
        "asset_class": "crypto",
        "timeframe": "1h",
        "symbols": ["BTC/USDT"],
        "indicators": [
            {"name": "EMA", "params": {"period": 20}},
            {"name": "EMA", "params": {"period": 50}},
            {"name": "RSI", "params": {"period": 14}},
        ],
        "long_conditions": [
            "EMA_20 crosses above EMA_50",
            "RSI(14) < 70",
        ],
        "short_conditions": [
            "EMA_20 crosses below EMA_50",
            "RSI(14) > 30",
        ],
        "exit_long_conditions":  ["EMA_20 crosses below EMA_50"],
        "exit_short_conditions": ["EMA_20 crosses above EMA_50"],
        "stop_loss":  {"type": "percent", "value": 2.0},
        "take_profit": {"type": "risk_reward", "ratio": 2.0},
        "position_sizing": {"type": "fixed_percent", "value": 5.0},
        "notes": "EMA crossover with RSI filter on BTC/USDT 1h",
        "source_transcript": "sample_ema_crossover.txt",
    }

    db.save_blueprint("ema_crossover_btc", blueprint_data, source_file="sample_ema_crossover.txt")
    console.print("[green]Blueprint injected:[/green] ema_crossover_btc")

    from strategies.generator import compile_strategy
    out_bt   = compile_strategy("ema_crossover_btc", mode="backtrader")
    out_ccxt = compile_strategy("ema_crossover_btc", mode="ccxt")
    db.update_status("ema_crossover_btc", "compiled")
    console.print(f"[green]Compiled (backtrader):[/green] {out_bt}")
    console.print(f"[green]Compiled (ccxt):[/green]       {out_ccxt}")

    console.print("\n[bold cyan]Running backtest on synthetic data...[/bold cyan]\n")
    import importlib.util, sys
    spec   = importlib.util.spec_from_file_location("bt_demo", out_bt)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bt_demo"] = module
    spec.loader.exec_module(module)
    module.run_backtest(cash=100_000.0, plot=False)

    console.print("\n[bold cyan]Running paper trade on synthetic data...[/bold cyan]\n")
    from execution.paper_trader import run_paper_trade
    result = run_paper_trade("ema_crossover_btc", speed=0.0)
    if result:
        db.log_deployment("ema_crossover_btc", "paper",
                          result.get("pnl", 0), result.get("trades", 0), result.get("log", ""))

    console.print(Panel(
        "[bold green]Demo complete![/bold green]\n\n"
        "Next steps:\n"
        "  1. Add your ANTHROPIC_API_KEY to .env\n"
        "  2. Drop a transcript .txt in /transcripts\n"
        "  3. python main.py parse <file.txt>\n"
        "  4. python main.py compile <name>\n"
        "  5. python main.py deploy <name>\n"
        "  Or: python main.py dashboard",
        title="[bold cyan]Getting Started[/bold cyan]",
        border_style="cyan",
    ))


if __name__ == "__main__":
    app()
