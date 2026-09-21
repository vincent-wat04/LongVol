from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Config, OptionQuote


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(spot: float, strike: float, years: float, rate: float,
                        dividend_yield: float, volatility: float, right: str) -> float:
    """European scenario price; used for conservative sizing, never as a quote."""
    if min(spot, strike) <= 0:
        return 0.0
    if years <= 0 or volatility <= 0:
        intrinsic = max(spot - strike, 0.0) if right == "C" else max(strike - spot, 0.0)
        return intrinsic
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * volatility ** 2) * years) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    if right == "C":
        return spot * math.exp(-dividend_yield * years) * _norm_cdf(d1) - strike * math.exp(-rate * years) * _norm_cdf(d2)
    return strike * math.exp(-rate * years) * _norm_cdf(-d2) - spot * math.exp(-dividend_yield * years) * _norm_cdf(-d1)


@dataclass(frozen=True)
class OptionScenario:
    target_option_price: float
    stop_option_price: float
    target_iv: float
    stop_iv: float
    horizon_days: int


def scenario_prices(option: OptionQuote, spot: float, target_spot: float,
                    hard_stop_spot: float, dte: int, config: Config) -> OptionScenario:
    horizon = max(1, min(config.target_horizon_days, dte - config.forced_expiry_exit_dte))
    years = max((dte - horizon) / 365.0, 1 / 365.0)
    target_iv = max(0.05, option.iv * (1 - config.target_iv_crush_pct))
    stop_iv = max(0.05, option.iv * (1 + config.stop_iv_change_pct))
    target = black_scholes_price(target_spot, option.strike, years, config.risk_free_rate,
                                 config.dividend_yield, target_iv, option.right)
    stop = black_scholes_price(hard_stop_spot, option.strike, years, config.risk_free_rate,
                               config.dividend_yield, stop_iv, option.right)
    # Scenario prices are intentionally conservative relative to tradable sides.
    target = max(target * 0.95, 0.01)
    stop = max(min(stop * 0.95, option.ask * (1 - config.option_premium_stop_pct)), 0.01)
    return OptionScenario(target, stop, target_iv, stop_iv, horizon)
