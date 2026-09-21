from __future__ import annotations

from dataclasses import dataclass

from .models import Position


@dataclass(frozen=True)
class PortfolioCapacity:
    eligible: bool
    reason: str
    open_positions: int
    committed_premium: float
    proposed_premium: float
    resulting_premium_fraction: float
    committed_risk: float
    proposed_risk: float
    resulting_risk_fraction: float
    committed_planned_risk: float = 0.0
    proposed_planned_risk: float = 0.0
    committed_full_premium_tail_risk: float = 0.0
    proposed_full_premium_tail_risk: float = 0.0


def check_portfolio_capacity(positions: list[Position], symbol: str, equity: float,
                             proposed_premium: float, proposed_risk: float,
                             max_total_premium_fraction: float, max_total_risk_fraction: float,
                             max_open_positions: int, max_positions_per_underlying: int) -> PortfolioCapacity:
    """Gate planned stop risk and full-premium tail risk independently.

    The existing ``*_risk`` fields retain their planned-stop meaning and are
    governed by ``max_total_risk_fraction``.  ``*_premium`` fields represent
    paid-premium capital/tail risk and are governed independently by
    ``max_total_premium_fraction``.
    """
    open_positions = [p for p in positions if p.status == "OPEN" and p.contracts > 0]
    committed = sum(
        (p.capital_at_risk_per_contract
         if p.capital_at_risk_per_contract is not None and
         p.capital_at_risk_per_contract > 0
         else p.entry_price * p.multiplier) * p.contracts
        for p in open_positions
    )
    committed_risk = sum(p.risk_per_contract * p.contracts
                         for p in open_positions)
    same_underlying = sum(1 for p in open_positions if p.symbol.upper() == symbol.upper())
    resulting = ((committed + proposed_premium) / equity) if equity > 0 else float("inf")
    resulting_risk = ((committed_risk + proposed_risk) / equity
                      if equity > 0 else float("inf"))
    reasons = []
    if any(p.currency != "USD" for p in open_positions):
        reasons.append("portfolio contains a non-USD position")
    if equity <= 0:
        reasons.append("invalid equity")
    if len(open_positions) >= max_open_positions:
        reasons.append("maximum open positions reached")
    if same_underlying >= max_positions_per_underlying:
        reasons.append("maximum positions for underlying reached")
    if resulting > max_total_premium_fraction:
        reasons.append("portfolio premium cap exceeded")
    if resulting_risk > max_total_risk_fraction:
        reasons.append("portfolio risk cap exceeded")
    return PortfolioCapacity(not reasons, "; ".join(reasons) or "ok", len(open_positions),
                             committed, proposed_premium, resulting,
                             committed_risk, proposed_risk, resulting_risk,
                             committed_risk, proposed_risk, committed,
                             proposed_premium)
