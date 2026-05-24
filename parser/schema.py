from pydantic import BaseModel, Field
from typing import Optional, Any


class Indicator(BaseModel):
    name: str
    params: dict[str, Any] = {}


class StopLoss(BaseModel):
    type: str = "percent"  # percent | atr | fixed | swing_low
    value: Optional[float] = None
    multiplier: Optional[float] = None  # for ATR-based stops


class TakeProfit(BaseModel):
    type: str = "risk_reward"  # percent | risk_reward | fixed | trailing
    value: Optional[float] = None
    ratio: Optional[float] = None  # for risk_reward type


class PositionSizing(BaseModel):
    type: str = "fixed_percent"  # fixed_percent | fixed_units | kelly
    value: float = 5.0


class StrategyBlueprint(BaseModel):
    name: str
    asset_class: str = "crypto"  # crypto | equities | forex | futures
    timeframe: str = "1h"
    symbols: list[str] = ["BTC/USDT"]
    indicators: list[Indicator] = []
    long_conditions: list[str] = []
    short_conditions: list[str] = []
    exit_long_conditions: list[str] = []
    exit_short_conditions: list[str] = []
    stop_loss: Optional[StopLoss] = None
    take_profit: Optional[TakeProfit] = None
    position_sizing: PositionSizing = Field(default_factory=PositionSizing)
    notes: str = ""
    source_transcript: str = ""
