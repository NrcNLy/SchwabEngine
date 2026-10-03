from pydantic import BaseModel, Field
from typing import Optional

class MarketEvent(BaseModel):
    ticker: str
    price: float
    atr: float
    choppiness_index: float
    timestamp: Optional[str] = None

class GovernorConfig(BaseModel):
    target_regime: str = Field(pattern="^(A|C)$", description="Target regime A (Breakout) or C (Mean Reversion)")
    macro_bias: str = Field(description="Bullish, Bearish, or Neutral")
    volatility_multiplier: float = Field(ge=0.5, le=2.0, description="Multiplier for risk sizing based on macro conditions")
