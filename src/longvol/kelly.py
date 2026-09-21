from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class KellyInput:
    equity: float
    win_probability: float
    entry_price: float
    target_price: float
    stop_price: float
    fees_per_contract: float = 0.0
    multiplier: int = 100
    fractional_kelly: float = 0.25
    max_allocation: float = 0.01
    max_premium_allocation: float = 0.03
    max_contracts: int = 10


@dataclass(frozen=True)
class FixedRiskInput:
    """Cold-start sizing with no assumed probability or Kelly edge."""

    equity: float
    entry_price: float
    target_price: float
    stop_price: float
    fees_per_contract: float = 0.0
    multiplier: int = 100
    max_risk_allocation: float = 0.025
    max_premium_allocation: float = 0.05
    max_contracts: int = 1


@dataclass(frozen=True)
class KellyResult:
    raw_kelly: float
    applied_fraction: float
    contracts: int
    capital_required: float
    b: float | None
    edge: float | None
    eligible: bool
    reason: str
    risk_per_contract: float = 0.0
    premium_fraction: float = 0.0
    mode: str = "VALIDATED_KELLY"
    capital_at_risk_per_contract: float = 0.0


def size_position(x: KellyInput) -> KellyResult:
    """Fractional Kelly for a defined binary approximation.

    `target_price` and `stop_price` are option prices, not underlying prices.
    This prevents an apparently cheap option from being sized using an
    optimistic underlying-only payoff assumption.
    """
    if x.equity <= 0 or not 0 < x.win_probability < 1:
        return KellyResult(0, 0, 0, 0, None, None, False, "invalid equity or win probability")
    if x.entry_price <= 0 or x.target_price <= x.entry_price or not 0 <= x.stop_price < x.entry_price:
        return KellyResult(0, 0, 0, 0, None, None, False, "invalid option entry/target/stop prices")
    round_trip_fees = 2 * x.fees_per_contract
    win = (x.target_price - x.entry_price) * x.multiplier - round_trip_fees
    loss = (x.entry_price - x.stop_price) * x.multiplier + round_trip_fees
    if win <= 0 or loss <= 0:
        return KellyResult(0, 0, 0, 0, None, None, False,
                           "option scenario has no positive net payoff after fees")
    b = win / loss
    q = 1 - x.win_probability
    raw = (b * x.win_probability - q) / b
    edge = b * x.win_probability - q
    # Keep two independent constraints: planned stop loss is the Kelly/risk
    # budget and R denominator; full premium plus entry fee is the tail-risk
    # capital/premium budget because the stop is not guaranteed.
    applied = max(0.0, min(raw * x.fractional_kelly, x.max_allocation))
    capital_at_risk = x.entry_price * x.multiplier + x.fees_per_contract
    risk_per_contract = loss
    by_risk = floor(x.equity * applied / risk_per_contract) if risk_per_contract > 0 else 0
    by_premium = floor(x.equity * x.max_premium_allocation / capital_at_risk) if capital_at_risk > 0 else 0
    contracts = max(0, min(by_risk, by_premium, x.max_contracts))
    eligible = raw > 0 and contracts > 0
    capital = contracts * capital_at_risk
    premium_fraction = capital / x.equity if x.equity else 0.0
    return KellyResult(raw, applied, contracts, capital, b, edge, eligible,
                       "ok" if eligible else "non-positive Kelly edge or planned-risk/full-premium cap permits zero contracts",
                       risk_per_contract, premium_fraction, "VALIDATED_KELLY",
                       capital_at_risk)


def size_fixed_risk(x: FixedRiskInput) -> KellyResult:
    """Size a cold-start pilot without manufacturing a win probability.

    Target/stop prices must still define a coherent option scenario. The
    position is capped independently by loss-to-stop, premium-at-risk, and
    whole-contract limits. Full premium remains the economic tail loss.
    """
    if x.equity <= 0:
        return KellyResult(0, 0, 0, 0, None, None, False, "invalid equity",
                           mode="COLD_START_FIXED_RISK")
    if x.entry_price <= 0 or x.target_price <= x.entry_price or not 0 <= x.stop_price < x.entry_price:
        return KellyResult(0, 0, 0, 0, None, None, False,
                           "invalid option entry/target/stop prices",
                           mode="COLD_START_FIXED_RISK")
    if (x.multiplier <= 0 or x.fees_per_contract < 0 or
            not 0 < x.max_risk_allocation < 1 or
            not 0 < x.max_premium_allocation < 1 or x.max_contracts <= 0):
        return KellyResult(0, 0, 0, 0, None, None, False,
                           "invalid cold-start sizing limits",
                           mode="COLD_START_FIXED_RISK")
    round_trip_fees = 2 * x.fees_per_contract
    win = (x.target_price - x.entry_price) * x.multiplier - round_trip_fees
    loss = (x.entry_price - x.stop_price) * x.multiplier + round_trip_fees
    if win <= 0 or loss <= 0:
        return KellyResult(0, 0, 0, 0, None, None, False,
                           "option scenario has no positive net payoff after fees",
                           mode="COLD_START_FIXED_RISK")
    capital_at_risk = x.entry_price * x.multiplier + x.fees_per_contract
    by_risk = floor(x.equity * x.max_risk_allocation / loss)
    by_premium = floor(x.equity * x.max_premium_allocation / capital_at_risk)
    contracts = max(0, min(by_risk, by_premium, x.max_contracts))
    capital = contracts * capital_at_risk
    applied = contracts * loss / x.equity
    premium_fraction = capital / x.equity
    return KellyResult(
        0.0, applied, contracts, capital, win / loss, None, contracts > 0,
        "cold-start fixed-risk cap" if contracts > 0 else
        "cold-start planned-risk/full-premium cap permits zero contracts",
        loss, premium_fraction, "COLD_START_FIXED_RISK", capital_at_risk,
    )


def size_position_input(x: KellyInput | FixedRiskInput) -> KellyResult:
    return size_position(x) if isinstance(x, KellyInput) else size_fixed_risk(x)
