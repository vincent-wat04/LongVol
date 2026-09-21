"""Conditional Monte Carlo economics for short-horizon reversal options.

The engine deliberately does not infer, fit, or optimize the equity signal.
An upstream, point-in-time stock model supplies a *fixed* conditional mixture
of rebound, stabilization, and continuation states.  Monte Carlo then answers
the narrower implementation question: given those assumptions and executable
option prices, which (if any) eligible contract has robust economics?

All randomness is local to ``simulate_option`` and seeded.  Ranking resets the
same seed for every contract, giving contracts common random numbers and making
results reproducible and independent of input ordering.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable

from .models import OptionQuote
from .pricing import black_scholes_price


TRADING_DAYS = 252.0
CALENDAR_DAYS = 365.0


@dataclass(frozen=True)
class RegimeState:
    """One externally supplied state in the conditional stock distribution.

    ``terminal_spot`` is the conditional median at the simulation horizon.
    ``volatility_multiplier`` scales the point-in-time annualized stock
    volatility. ``terminal_iv_multiplier`` is the IV mean-reversion anchor
    relative to the contract's entry IV.
    """

    name: str
    probability: float
    terminal_spot: float
    volatility_multiplier: float
    terminal_iv_multiplier: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("state name is required")
        if not 0 <= self.probability <= 1:
            raise ValueError("state probability must be in [0, 1]")
        if self.terminal_spot <= 0:
            raise ValueError("state terminal_spot must be positive")
        if self.volatility_multiplier <= 0:
            raise ValueError("state volatility_multiplier must be positive")
        if self.terminal_iv_multiplier <= 0:
            raise ValueError("state terminal_iv_multiplier must be positive")


@dataclass(frozen=True)
class UnderlyingDistribution:
    """Point-in-time conditional distribution supplied by the stock process.

    This object is a condition to the option simulation, not an output of it.
    Probabilities should ultimately come from frozen out-of-sample estimates
    (with shrinkage), never from selecting the option that backtests best.
    """

    spot: float
    atr: float
    target_spot: float
    hard_stop_spot: float
    horizon_days: int
    annualized_volatility: float
    states: tuple[RegimeState, ...]
    assumption_source: str = "UNVALIDATED_PRIOR"
    out_of_sample_validated: bool = False

    def __post_init__(self) -> None:
        if self.spot <= 0 or self.atr <= 0:
            raise ValueError("spot and atr must be positive")
        if not self.hard_stop_spot < self.spot < self.target_spot:
            raise ValueError("a bullish reversal requires stop < spot < target")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.annualized_volatility <= 0:
            raise ValueError("annualized_volatility must be positive")
        if not self.states:
            raise ValueError("at least one regime state is required")
        probability = sum(state.probability for state in self.states)
        if not math.isclose(probability, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("state probabilities must sum to 1")
        names = [state.name for state in self.states]
        if len(set(names)) != len(names):
            raise ValueError("state names must be unique")
        if not self.assumption_source:
            raise ValueError("assumption_source is required for auditability")


def build_reversal_distribution(
    *,
    spot: float,
    atr: float,
    target_spot: float,
    hard_stop_spot: float,
    horizon_days: int,
    annualized_volatility: float,
    rebound_probability: float,
    continuation_probability: float,
    rebound_overshoot_atr: float = 0.25,
    continuation_overshoot_atr: float = 0.25,
    stabilization_drift_atr: float = 0.0,
    rebound_volatility_multiplier: float = 0.90,
    stabilization_volatility_multiplier: float = 0.60,
    continuation_volatility_multiplier: float = 1.25,
    rebound_iv_crush: float = 0.15,
    stabilization_iv_crush: float = 0.10,
    continuation_iv_expansion: float = 0.10,
    assumption_source: str = "UNVALIDATED_PRIOR",
    out_of_sample_validated: bool = False,
) -> UnderlyingDistribution:
    """Build the standard three-state reversal mixture.

    The two supplied probabilities must be pre-specified stock-model inputs;
    the residual probability is stabilization/no meaningful rebound.  The
    defaults are transparent scenario-shape assumptions, not calibrated edge.
    """

    probability_sum = rebound_probability + continuation_probability
    if rebound_probability < 0 or continuation_probability < 0 or probability_sum > 1.0 + 1e-9:
        raise ValueError("rebound and continuation probabilities must be non-negative and sum to at most 1")
    stabilization_probability = max(0.0, 1.0 - probability_sum)
    if min(rebound_overshoot_atr, continuation_overshoot_atr) < 0:
        raise ValueError("state overshoot assumptions must be non-negative")
    if not 0 <= rebound_iv_crush < 1 or not 0 <= stabilization_iv_crush < 1:
        raise ValueError("IV crush fractions must be in [0, 1)")
    if continuation_iv_expansion < -1:
        raise ValueError("continuation_iv_expansion must be greater than -1")

    states = (
        RegimeState(
            "REBOUND",
            rebound_probability,
            target_spot + rebound_overshoot_atr * atr,
            rebound_volatility_multiplier,
            1.0 - rebound_iv_crush,
        ),
        RegimeState(
            "STABILIZATION",
            stabilization_probability,
            max(0.01, spot + stabilization_drift_atr * atr),
            stabilization_volatility_multiplier,
            1.0 - stabilization_iv_crush,
        ),
        RegimeState(
            "CONTINUATION",
            continuation_probability,
            max(0.01, hard_stop_spot - continuation_overshoot_atr * atr),
            continuation_volatility_multiplier,
            1.0 + continuation_iv_expansion,
        ),
    )
    return UnderlyingDistribution(
        spot=spot,
        atr=atr,
        target_spot=target_spot,
        hard_stop_spot=hard_stop_spot,
        horizon_days=horizon_days,
        annualized_volatility=annualized_volatility,
        states=states,
        assumption_source=assumption_source,
        out_of_sample_validated=out_of_sample_validated,
    )


@dataclass(frozen=True)
class SimulationSettings:
    paths: int = 10_000
    seed: int = 17
    risk_free_rate: float = 0.04
    dividend_yield: float = 0.0
    iv_half_life_days: float = 5.0
    annualized_iv_noise: float = 0.20
    spot_iv_correlation: float = -0.60
    minimum_iv: float = 0.05
    maximum_iv: float = 3.00
    minimum_exit_haircut: float = 0.05
    stressed_exit_haircut: float = 0.15
    calibrate_model_to_mid: bool = True
    premium_stop_loss_fraction: float | None = None
    entry_fee_per_contract: float = 0.0
    exit_fee_per_contract: float = 0.0
    var_confidence: float = 0.95
    cvar_penalty_weight: float = 0.25

    def __post_init__(self) -> None:
        if self.paths <= 0:
            raise ValueError("paths must be positive")
        if self.iv_half_life_days <= 0 or self.annualized_iv_noise < 0:
            raise ValueError("IV half-life must be positive and IV noise non-negative")
        if not -1 <= self.spot_iv_correlation <= 1:
            raise ValueError("spot_iv_correlation must be in [-1, 1]")
        if not 0 < self.minimum_iv <= self.maximum_iv:
            raise ValueError("invalid IV bounds")
        if not 0 <= self.minimum_exit_haircut < 1:
            raise ValueError("minimum_exit_haircut must be in [0, 1)")
        if not 0 <= self.stressed_exit_haircut < 1:
            raise ValueError("stressed_exit_haircut must be in [0, 1)")
        if self.premium_stop_loss_fraction is not None and not 0 < self.premium_stop_loss_fraction < 1:
            raise ValueError("premium_stop_loss_fraction must be in (0, 1)")
        if min(self.entry_fee_per_contract, self.exit_fee_per_contract) < 0:
            raise ValueError("fees must be non-negative")
        if not 0.5 < self.var_confidence < 1:
            raise ValueError("var_confidence must be in (0.5, 1)")
        if self.cvar_penalty_weight < 0:
            raise ValueError("cvar_penalty_weight must be non-negative")


@dataclass(frozen=True)
class ContractFilter:
    """Execution/liquidity eligibility only; it contains no alpha score."""

    right: str = "C"
    min_dte: int = 1
    max_dte: int = 3650
    min_exit_dte: int = 0
    min_abs_delta: float = 0.0
    max_abs_delta: float = 1.0
    min_open_interest: float = 0.0
    min_volume: float = 0.0
    max_spread_pct: float = 1.0
    max_premium_to_spot: float = 1.0
    require_quote_on_as_of: bool = False

    def __post_init__(self) -> None:
        if self.right not in {"C", "P"}:
            raise ValueError("right must be C or P")
        if self.min_dte < 0 or self.max_dte < self.min_dte or self.min_exit_dte < 0:
            raise ValueError("invalid DTE bounds")
        if not 0 <= self.min_abs_delta <= self.max_abs_delta <= 1:
            raise ValueError("invalid delta bounds")
        if min(self.min_open_interest, self.min_volume, self.max_spread_pct,
               self.max_premium_to_spot) < 0:
            raise ValueError("liquidity thresholds must be non-negative")


@dataclass(frozen=True)
class EconomicPolicy:
    """Explicit deployment floors applied after every eligible quote is run."""

    min_expected_return: float | None = 0.0
    min_probability_profit: float | None = 0.50
    min_target_scenario_return: float | None = 0.0
    min_robust_score: float | None = None
    max_cvar_capital_fraction: float | None = None
    require_oos_distribution: bool = False

    def __post_init__(self) -> None:
        if self.min_probability_profit is not None and not 0 <= self.min_probability_profit <= 1:
            raise ValueError("min_probability_profit must be in [0, 1]")
        if self.max_cvar_capital_fraction is not None and self.max_cvar_capital_fraction < 0:
            raise ValueError("max_cvar_capital_fraction must be non-negative")


@dataclass(frozen=True)
class ScenarioOutcome:
    name: str
    day: int
    spot: float
    iv: float
    theoretical_option_price: float
    executable_option_price: float
    pnl_per_contract: float
    return_on_capital: float
    r_multiple: float | None


@dataclass(frozen=True)
class StateOutcome:
    name: str
    assumed_probability: float
    paths: int
    expected_return: float | None
    probability_profit: float | None
    target_exit_probability: float | None
    stop_exit_probability: float | None


@dataclass(frozen=True)
class OptionSimulation:
    option: OptionQuote
    distribution: UnderlyingDistribution
    settings: SimulationSettings
    paths: int
    seed: int
    entry_price: float
    entry_cost_per_contract: float
    full_premium_capital_risk: float
    planned_risk_per_contract: float | None
    modeled_stop_loss_per_contract: float | None
    operational_premium_stop_price: float | None
    operational_premium_stop_loss_per_contract: float | None
    planned_risk_basis: str
    model_price_scale: float
    exit_haircut: float
    expected_pnl_per_contract: float
    expected_return: float
    expected_r_multiple: float | None
    median_return: float
    probability_profit: float
    var_loss_per_contract: float
    cvar_loss_per_contract: float
    var_capital_fraction: float
    cvar_capital_fraction: float
    p05_return: float
    p95_return: float
    robust_score: float | None
    target_exit_probability: float
    stop_exit_probability: float
    premium_stop_exit_probability: float
    time_exit_probability: float
    mean_exit_day: float
    target_scenario: ScenarioOutcome
    stop_scenario: ScenarioOutcome
    state_outcomes: tuple[StateOutcome, ...]
    passes_economic_policy: bool = True
    economic_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContractRejection:
    symbol: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class OptionRanking:
    ranked: tuple[OptionSimulation, ...]
    rejected: tuple[ContractRejection, ...]


def _observed_bid_haircut(option: OptionQuote) -> float:
    if option.mid <= 0:
        return 1.0
    return min(max(1.0 - option.bid / option.mid, 0.0), 1.0)


def _model_scale(option: OptionQuote, spot: float, dte: int, settings: SimulationSettings) -> float:
    if not settings.calibrate_model_to_mid:
        return 1.0
    raw = black_scholes_price(
        spot,
        option.strike,
        dte / CALENDAR_DAYS,
        settings.risk_free_rate,
        settings.dividend_yield,
        option.iv,
        option.right,
    )
    return option.mid / raw if raw > 0 and option.mid > 0 else 1.0


def _marked_price(
    option: OptionQuote,
    spot: float,
    iv: float,
    initial_dte: int,
    elapsed_trading_days: int,
    scale: float,
    settings: SimulationSettings,
) -> float:
    years = max(initial_dte / CALENDAR_DAYS - elapsed_trading_days / TRADING_DAYS, 0.0)
    raw = black_scholes_price(
        spot,
        option.strike,
        years,
        settings.risk_free_rate,
        settings.dividend_yield,
        iv,
        option.right,
    )
    intrinsic = (max(spot - option.strike, 0.0) if option.right == "C"
                 else max(option.strike - spot, 0.0))
    return max(raw * scale, intrinsic)


def _exit_price(mark: float, haircut: float) -> float:
    return max(mark * (1.0 - haircut), 0.0)


def _proceeds(executable_price: float, option: OptionQuote, settings: SimulationSettings) -> float:
    # A mandated liquidation can cost more than the residual option proceeds.
    # Do not silently waive the exit fee: sizing uses the same round-trip-fee
    # convention, including for near-worthless contracts.
    return executable_price * option.multiplier - settings.exit_fee_per_contract


def _select_state(states: tuple[RegimeState, ...], draw: float) -> RegimeState:
    cumulative = 0.0
    for state in states:
        cumulative += state.probability
        if draw < cumulative:
            return state
    return states[-1]


def _scenario(
    name: str,
    option: OptionQuote,
    day: int,
    spot: float,
    iv: float,
    dte: int,
    scale: float,
    haircut: float,
    entry_cost: float,
    planned_risk: float | None,
    settings: SimulationSettings,
) -> ScenarioOutcome:
    mark = _marked_price(option, spot, iv, dte, day, scale, settings)
    executable = _exit_price(mark, haircut)
    pnl = _proceeds(executable, option, settings) - entry_cost
    return ScenarioOutcome(
        name=name,
        day=day,
        spot=spot,
        iv=iv,
        theoretical_option_price=mark,
        executable_option_price=executable,
        pnl_per_contract=pnl,
        return_on_capital=pnl / entry_cost,
        r_multiple=(pnl / planned_risk if planned_risk is not None and planned_risk > 0 else None),
    )


def _percentile(sorted_values: list[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def simulate_option(
    option: OptionQuote,
    as_of: date,
    distribution: UnderlyingDistribution,
    settings: SimulationSettings = SimulationSettings(),
) -> OptionSimulation:
    """Simulate one long option from ask entry to conservative executable exit."""

    dte = (option.expiry - as_of).days
    if dte <= 0:
        raise ValueError("option must not be expired as of the simulation date")
    if option.ask <= 0 or option.bid < 0 or option.ask < option.bid:
        raise ValueError("option quote is not executable")
    if option.iv <= 0 or option.multiplier <= 0:
        raise ValueError("option IV and multiplier must be positive")

    entry_cost = option.ask * option.multiplier + settings.entry_fee_per_contract
    capital_risk = entry_cost
    scale = _model_scale(option, distribution.spot, dte, settings)
    normal_haircut = max(settings.minimum_exit_haircut, _observed_bid_haircut(option))
    stressed_haircut = max(normal_haircut, settings.stressed_exit_haircut)
    iv_alpha = 1.0 - math.exp(-math.log(2.0) / settings.iv_half_life_days)
    independent_iv_weight = math.sqrt(max(0.0, 1.0 - settings.spot_iv_correlation ** 2))
    daily_iv_noise = settings.annualized_iv_noise / math.sqrt(TRADING_DAYS)

    rng = random.Random(settings.seed)
    pnls: list[float] = []
    exit_days: list[int] = []
    exit_counts = {"TARGET": 0, "HARD_STOP": 0, "PREMIUM_STOP": 0, "TIME": 0}
    state_pnls: dict[str, list[float]] = {state.name: [] for state in distribution.states}
    state_exits: dict[str, dict[str, int]] = {
        state.name: {"TARGET": 0, "HARD_STOP": 0, "PREMIUM_STOP": 0, "TIME": 0}
        for state in distribution.states
    }

    for _ in range(settings.paths):
        # Draw the entire path up front so early option-specific exits do not
        # disturb common random numbers used to compare different contracts.
        state_draw = rng.random()
        spot_shocks = [rng.gauss(0.0, 1.0) for _day in range(distribution.horizon_days)]
        iv_shocks = [rng.gauss(0.0, 1.0) for _day in range(distribution.horizon_days)]
        state = _select_state(distribution.states, state_draw)
        state_sigma = distribution.annualized_volatility * state.volatility_multiplier
        daily_sigma = state_sigma / math.sqrt(TRADING_DAYS)
        daily_log_drift = math.log(state.terminal_spot / distribution.spot) / distribution.horizon_days
        iv_anchor = min(max(option.iv * state.terminal_iv_multiplier, settings.minimum_iv), settings.maximum_iv)
        path_spot = distribution.spot
        path_iv = option.iv
        pnl = -entry_cost
        exit_reason = "TIME"
        exit_day = distribution.horizon_days

        for day, (spot_z, independent_iv_z) in enumerate(zip(spot_shocks, iv_shocks), start=1):
            path_spot = max(
                0.01,
                # terminal_spot is defined as the conditional median, so the
                # log process does not apply the mean-preserving -sigma^2/2.
                path_spot * math.exp(daily_log_drift + daily_sigma * spot_z),
            )
            correlated_iv_z = (settings.spot_iv_correlation * spot_z
                               + independent_iv_weight * independent_iv_z)
            path_iv += iv_alpha * (iv_anchor - path_iv) + daily_iv_noise * correlated_iv_z
            path_iv = min(max(path_iv, settings.minimum_iv), settings.maximum_iv)

            if path_spot <= distribution.hard_stop_spot:
                exit_reason = "HARD_STOP"
            elif path_spot >= distribution.target_spot:
                exit_reason = "TARGET"
            else:
                exit_reason = "TIME"

            mark = _marked_price(option, path_spot, path_iv, dte, day, scale, settings)
            current_haircut = stressed_haircut if exit_reason == "HARD_STOP" else normal_haircut
            executable = _exit_price(mark, current_haircut)
            if (exit_reason == "TIME" and settings.premium_stop_loss_fraction is not None
                    and executable <= option.ask * (1.0 - settings.premium_stop_loss_fraction)):
                exit_reason = "PREMIUM_STOP"
                executable = _exit_price(mark, stressed_haircut)

            if exit_reason != "TIME" or day == distribution.horizon_days:
                pnl = _proceeds(executable, option, settings) - entry_cost
                exit_day = day
                break

        pnls.append(pnl)
        exit_days.append(exit_day)
        exit_counts[exit_reason] += 1
        state_pnls[state.name].append(pnl)
        state_exits[state.name][exit_reason] += 1

    sorted_pnls = sorted(pnls)
    expected_pnl = sum(pnls) / len(pnls)
    tail_probability = 1.0 - settings.var_confidence
    tail_count = max(1, math.ceil(tail_probability * len(sorted_pnls)))
    var_loss = max(0.0, -sorted_pnls[tail_count - 1])
    cvar_loss = max(0.0, -sum(sorted_pnls[:tail_count]) / tail_count)
    p05 = _percentile(sorted_pnls, 0.05) / capital_risk
    p95 = _percentile(sorted_pnls, 0.95) / capital_risk
    target_state = min(distribution.states, key=lambda item: abs(item.terminal_spot - distribution.target_spot))
    stop_state = min(distribution.states, key=lambda item: abs(item.terminal_spot - distribution.hard_stop_spot))
    target_scenario = _scenario(
        "TARGET_AT_HORIZON",
        option,
        distribution.horizon_days,
        distribution.target_spot,
        min(settings.maximum_iv,
            max(settings.minimum_iv, option.iv * target_state.terminal_iv_multiplier)),
        dte,
        scale,
        normal_haircut,
        entry_cost,
        None,
        settings,
    )
    stop_scenario = _scenario(
        "STOP_AT_HORIZON",
        option,
        distribution.horizon_days,
        distribution.hard_stop_spot,
        min(settings.maximum_iv,
            max(settings.minimum_iv, option.iv * stop_state.terminal_iv_multiplier)),
        dte,
        scale,
        stressed_haircut,
        entry_cost,
        None,
        settings,
    )
    modeled_stop_loss = (
        -stop_scenario.pnl_per_contract
        if stop_scenario.pnl_per_contract < 0 else None
    )
    planned_risk = modeled_stop_loss
    operational_stop_price = (
        option.ask * (1.0 - settings.premium_stop_loss_fraction)
        if settings.premium_stop_loss_fraction is not None else None
    )
    operational_stop_loss = (
        option.ask * option.multiplier * settings.premium_stop_loss_fraction
        + settings.entry_fee_per_contract + settings.exit_fee_per_contract
        if settings.premium_stop_loss_fraction is not None else None
    )
    if planned_risk is not None:
        target_scenario = replace(
            target_scenario,
            r_multiple=target_scenario.pnl_per_contract / planned_risk,
        )
        stop_scenario = replace(
            stop_scenario,
            r_multiple=stop_scenario.pnl_per_contract / planned_risk,
        )
        expected_r = expected_pnl / planned_risk
        cvar_r = cvar_loss / planned_risk
        robust_score = expected_r - settings.cvar_penalty_weight * cvar_r
    else:
        expected_r = None
        robust_score = None

    state_outcomes: list[StateOutcome] = []
    for state in distribution.states:
        values = state_pnls[state.name]
        count = len(values)
        state_outcomes.append(StateOutcome(
            name=state.name,
            assumed_probability=state.probability,
            paths=count,
            expected_return=(sum(values) / count / capital_risk if count else None),
            probability_profit=(sum(value > 0 for value in values) / count if count else None),
            target_exit_probability=(state_exits[state.name]["TARGET"] / count if count else None),
            stop_exit_probability=(
                (state_exits[state.name]["HARD_STOP"] + state_exits[state.name]["PREMIUM_STOP"]) / count
                if count else None
            ),
        ))

    return OptionSimulation(
        option=option,
        distribution=distribution,
        settings=settings,
        paths=settings.paths,
        seed=settings.seed,
        entry_price=option.ask,
        entry_cost_per_contract=entry_cost,
        full_premium_capital_risk=capital_risk,
        planned_risk_per_contract=planned_risk,
        modeled_stop_loss_per_contract=modeled_stop_loss,
        operational_premium_stop_price=operational_stop_price,
        operational_premium_stop_loss_per_contract=operational_stop_loss,
        planned_risk_basis="DETERMINISTIC_MODELED_STOP_NET_LOSS",
        model_price_scale=scale,
        exit_haircut=normal_haircut,
        expected_pnl_per_contract=expected_pnl,
        expected_return=expected_pnl / capital_risk,
        expected_r_multiple=expected_r,
        median_return=_percentile(sorted_pnls, 0.50) / capital_risk,
        probability_profit=sum(value > 0 for value in pnls) / len(pnls),
        var_loss_per_contract=var_loss,
        cvar_loss_per_contract=cvar_loss,
        var_capital_fraction=var_loss / capital_risk,
        cvar_capital_fraction=cvar_loss / capital_risk,
        p05_return=p05,
        p95_return=p95,
        robust_score=robust_score,
        target_exit_probability=exit_counts["TARGET"] / settings.paths,
        stop_exit_probability=exit_counts["HARD_STOP"] / settings.paths,
        premium_stop_exit_probability=exit_counts["PREMIUM_STOP"] / settings.paths,
        time_exit_probability=exit_counts["TIME"] / settings.paths,
        mean_exit_day=sum(exit_days) / len(exit_days),
        target_scenario=target_scenario,
        stop_scenario=stop_scenario,
        state_outcomes=tuple(state_outcomes),
    )


def contract_rejection_reasons(
    option: OptionQuote,
    as_of: date,
    distribution: UnderlyingDistribution,
    contract_filter: ContractFilter,
) -> tuple[str, ...]:
    reasons: list[str] = []
    dte = (option.expiry - as_of).days
    elapsed_calendar_days = math.ceil(distribution.horizon_days * CALENDAR_DAYS / TRADING_DAYS)
    if option.right != contract_filter.right:
        reasons.append("RIGHT")
    if dte < contract_filter.min_dte or dte > contract_filter.max_dte:
        reasons.append("DTE")
    if dte - elapsed_calendar_days < contract_filter.min_exit_dte:
        reasons.append("EXIT_DTE_BUFFER")
    if option.bid < 0 or option.ask <= 0 or option.ask < option.bid or option.mid <= 0:
        reasons.append("INVALID_QUOTE")
    if option.spread_pct > contract_filter.max_spread_pct:
        reasons.append("SPREAD")
    if option.iv <= 0:
        reasons.append("IV")
    if not contract_filter.min_abs_delta <= abs(option.delta) <= contract_filter.max_abs_delta:
        reasons.append("DELTA")
    if option.open_interest < contract_filter.min_open_interest:
        reasons.append("OPEN_INTEREST")
    if option.volume < contract_filter.min_volume:
        reasons.append("VOLUME")
    if option.ask / distribution.spot > contract_filter.max_premium_to_spot:
        reasons.append("PREMIUM_TO_SPOT")
    if (contract_filter.require_quote_on_as_of
            and (option.quote_time is None or option.quote_time.date() != as_of)):
        reasons.append("QUOTE_DATE")
    return tuple(reasons)


def _economic_reasons(result: OptionSimulation, policy: EconomicPolicy) -> tuple[str, ...]:
    reasons: list[str] = []
    if policy.require_oos_distribution and not result.distribution.out_of_sample_validated:
        reasons.append("DISTRIBUTION_NOT_OOS_VALIDATED")
    if policy.min_expected_return is not None and result.expected_return < policy.min_expected_return:
        reasons.append("EXPECTED_RETURN")
    if (policy.min_probability_profit is not None
            and result.probability_profit < policy.min_probability_profit):
        reasons.append("PROBABILITY_PROFIT")
    if (policy.min_target_scenario_return is not None
            and result.target_scenario.return_on_capital < policy.min_target_scenario_return):
        reasons.append("TARGET_SCENARIO_RETURN")
    if (policy.min_robust_score is not None and
            (result.robust_score is None or result.robust_score < policy.min_robust_score)):
        reasons.append("ROBUST_SCORE")
    if (policy.max_cvar_capital_fraction is not None
            and result.cvar_capital_fraction > policy.max_cvar_capital_fraction):
        reasons.append("CVAR")
    return tuple(reasons)


def rank_option_contracts(
    options: Iterable[OptionQuote],
    as_of: date,
    distribution: UnderlyingDistribution,
    settings: SimulationSettings = SimulationSettings(),
    contract_filter: ContractFilter = ContractFilter(),
    economic_policy: EconomicPolicy = EconomicPolicy(),
) -> OptionRanking:
    """Run every execution-eligible quote, then rank by tail-penalized economics."""

    simulations: list[OptionSimulation] = []
    rejected: list[ContractRejection] = []
    for option in options:
        reasons = contract_rejection_reasons(option, as_of, distribution, contract_filter)
        if reasons:
            rejected.append(ContractRejection(option.symbol, reasons))
            continue
        result = simulate_option(option, as_of, distribution, settings)
        economic_reasons = _economic_reasons(result, economic_policy)
        simulations.append(replace(
            result,
            passes_economic_policy=not economic_reasons,
            economic_reasons=economic_reasons,
        ))

    simulations.sort(key=lambda result: (
        not result.passes_economic_policy,
        -(result.robust_score if result.robust_score is not None else float("-inf")),
        -result.expected_return,
        -result.probability_profit,
        result.option.spread_pct,
        result.option.symbol,
    ))
    rejected.sort(key=lambda item: item.symbol)
    return OptionRanking(tuple(simulations), tuple(rejected))
