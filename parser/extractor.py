import json
import re
from pathlib import Path

import anthropic
from rich.console import Console

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from parser.schema import StrategyBlueprint

console = Console()

SYSTEM_PROMPT = """You are an expert quantitative analyst. Your job is to extract a precise
algorithmic trading strategy from a video transcript or written description.

Return ONLY a valid JSON object — no markdown fences, no explanation, no extra text.

The JSON must conform exactly to this schema:
{
  "name": "string (short slug, e.g. ema_crossover_btc)",
  "asset_class": "crypto | equities | forex | futures",
  "timeframe": "1m | 5m | 15m | 30m | 1h | 4h | 1d | 1w",
  "symbols": ["list of symbols, e.g. BTC/USDT"],
  "indicators": [
    {"name": "INDICATOR_NAME", "params": {"period": 20, "source": "close"}}
  ],
  "long_conditions": ["human-readable condition strings"],
  "short_conditions": ["human-readable condition strings"],
  "exit_long_conditions": ["conditions to close a long position"],
  "exit_short_conditions": ["conditions to close a short position"],
  "stop_loss": {
    "type": "percent | atr | fixed | swing_low",
    "value": 2.0,
    "multiplier": null
  },
  "take_profit": {
    "type": "percent | risk_reward | fixed | trailing",
    "value": null,
    "ratio": 2.0
  },
  "position_sizing": {
    "type": "fixed_percent | fixed_units | kelly",
    "value": 5.0
  },
  "notes": "any extra context about the strategy"
}

Rules:
- If information is not present, use sensible defaults or null.
- Condition strings should be precise: e.g. "EMA_20 crosses above EMA_50", "RSI(14) < 30", "close > upper_BB".
- Indicator names: EMA, SMA, RSI, MACD, BB (Bollinger Bands), ATR, Stochastic, ADX, VWAP, ICHIMOKU.
- The name must be a valid Python identifier (lowercase, underscores).
"""


def parse_transcript(transcript_path: str | Path, strategy_name: str | None = None) -> StrategyBlueprint:
    path = Path(transcript_path)
    if not path.exists():
        raise FileNotFoundError(f"Transcript not found: {path}")

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("Transcript file is empty.")

    if not ANTHROPIC_API_KEY:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not set. Add it to your .env file."
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_message = f"""Extract the trading strategy from the following transcript.

TRANSCRIPT:
---
{text}
---

Return the JSON blueprint now."""

    console.print("[dim]Calling Claude to parse transcript...[/dim]")

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = message.content[0].text.strip()

    # Strip any accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON: {e}\n\nRaw output:\n{raw}")

    # Override name if provided
    if strategy_name:
        data["name"] = strategy_name

    data["source_transcript"] = path.name

    blueprint = StrategyBlueprint.model_validate(data)
    return blueprint
