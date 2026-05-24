"""
Interactive terminal dashboard — Rich-powered menu wrapping all CLI commands.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text

import db
from config import TRANSCRIPTS_DIR, STRATEGIES_DIR

console = Console()

BANNER = """
[bold cyan]
  ████████╗██████╗  █████╗ ██████╗ ███████╗████████╗███████╗███████╗████████╗
  ╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝
     ██║   ██████╔╝███████║██║  ██║█████╗     ██║   █████╗  ███████╗   ██║
     ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝     ██║   ██╔══╝  ╚════██║   ██║
     ██║   ██║  ██║██║  ██║██████╔╝███████╗   ██║   ███████╗███████║   ██║
     ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝   ╚═╝   ╚══════╝╚══════╝   ╚═╝
[/bold cyan]
[dim]  Transcript → Strategy → Backtest → Deploy[/dim]
"""

MENU_ITEMS = [
    ("1", "List Transcripts",           "list_transcripts"),
    ("2", "Parse Transcript → Blueprint","do_parse"),
    ("3", "List Strategies",            "list_strategies"),
    ("4", "Review Strategy Blueprint",  "do_review"),
    ("5", "Compile Strategy",           "do_compile"),
    ("6", "Run Backtest",               "do_backtest"),
    ("7", "Paper Trade (mock)",         "do_paper_trade"),
    ("q", "Quit",                       "quit"),
]


def _header() -> None:
    console.clear()
    console.print(BANNER)
    console.rule("[bold cyan]Main Menu[/bold cyan]")


def _menu_panel() -> None:
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column("Key",    style="bold yellow", width=4)
    tbl.add_column("Action", style="white")
    for key, label, _ in MENU_ITEMS:
        tbl.add_row(f"[{key}]", label)
    console.print(tbl)
    console.print()


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def list_transcripts() -> None:
    files = sorted(TRANSCRIPTS_DIR.glob("*.txt"))
    if not files:
        console.print("[yellow]No transcript files found in /transcripts[/yellow]")
        console.print("[dim]Place .txt files there and re-open the dashboard.[/dim]")
        return

    tbl = Table(title="Available Transcripts", border_style="cyan")
    tbl.add_column("#",    style="dim", width=4)
    tbl.add_column("File", style="bold")
    tbl.add_column("Size", style="dim")
    for i, f in enumerate(files, 1):
        tbl.add_row(str(i), f.name, f"{f.stat().st_size:,} bytes")
    console.print(tbl)


def do_parse() -> None:
    list_transcripts()
    files = sorted(TRANSCRIPTS_DIR.glob("*.txt"))
    if not files:
        return

    fname = Prompt.ask("\nEnter transcript filename (without path)")
    path  = TRANSCRIPTS_DIR / fname
    if not path.exists():
        console.print(f"[red]File not found: {path}[/red]")
        return

    name  = Prompt.ask("Strategy name (leave blank to auto-derive)")
    args  = ["python", "main.py", "parse", str(path)]
    if name.strip():
        args += ["--name", name.strip()]

    console.print(f"\n[dim]Running: {' '.join(args)}[/dim]\n")
    subprocess.run(args, cwd=Path(__file__).parent.parent)


def list_strategies() -> None:
    rows = db.list_strategies()
    if not rows:
        console.print("[yellow]No strategies in database yet.[/yellow]")
        return

    tbl = Table(title="Strategies", border_style="green")
    tbl.add_column("Name",       style="bold")
    tbl.add_column("Status",     style="cyan")
    tbl.add_column("Source",     style="dim")
    tbl.add_column("Created",    style="dim")
    status_color = {"parsed": "yellow", "compiled": "cyan", "deployed": "green"}
    for r in rows:
        color  = status_color.get(r["status"], "white")
        tbl.add_row(
            r["name"],
            Text(r["status"], style=color),
            r.get("source_file") or "—",
            r["created_at"][:16],
        )
    console.print(tbl)


def do_review() -> None:
    list_strategies()
    rows = db.list_strategies()
    if not rows:
        return

    name = Prompt.ask("\nStrategy name to review")
    subprocess.run(["python", "main.py", "review", name],
                   cwd=Path(__file__).parent.parent)


def do_compile() -> None:
    list_strategies()
    rows = db.list_strategies()
    if not rows:
        return

    name = Prompt.ask("\nStrategy name to compile")
    mode = Prompt.ask("Mode", choices=["backtrader", "ccxt"], default="backtrader")
    subprocess.run(["python", "main.py", "compile", name, "--mode", mode],
                   cwd=Path(__file__).parent.parent)


def do_backtest() -> None:
    files = sorted(STRATEGIES_DIR.glob("*_backtrader.py"))
    if not files:
        console.print("[yellow]No compiled backtrader strategies found. Run compile first.[/yellow]")
        return

    tbl = Table(title="Compiled Backtrader Strategies", border_style="cyan")
    tbl.add_column("#"); tbl.add_column("File")
    for i, f in enumerate(files, 1):
        tbl.add_row(str(i), f.name)
    console.print(tbl)

    name = Prompt.ask("\nStrategy name")
    subprocess.run(["python", "main.py", "backtest", name],
                   cwd=Path(__file__).parent.parent)


def do_paper_trade() -> None:
    list_strategies()
    rows = db.list_strategies()
    if not rows:
        return

    name = Prompt.ask("\nStrategy name to paper trade")
    subprocess.run(["python", "main.py", "deploy", name],
                   cwd=Path(__file__).parent.parent)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_ACTIONS = {key: fn for key, _, fn in MENU_ITEMS}
_HANDLERS = {
    "list_transcripts": list_transcripts,
    "do_parse":         do_parse,
    "list_strategies":  list_strategies,
    "do_review":        do_review,
    "do_compile":       do_compile,
    "do_backtest":      do_backtest,
    "do_paper_trade":   do_paper_trade,
}


def run_dashboard() -> None:
    while True:
        _header()
        _menu_panel()
        choice = Prompt.ask("[bold yellow]Choose[/bold yellow]",
                            choices=[k for k, _, _ in MENU_ITEMS],
                            show_choices=False).strip().lower()

        fn_name = _ACTIONS.get(choice)
        if fn_name == "quit":
            console.print("\n[bold cyan]Goodbye.[/bold cyan]\n")
            break

        handler = _HANDLERS.get(fn_name or "")
        if handler:
            console.print()
            handler()
            console.print()
            Prompt.ask("[dim]Press Enter to return to menu[/dim]", default="")
        else:
            console.print("[red]Invalid choice.[/red]")
