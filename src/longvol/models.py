from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime | None = None


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    expiry: date
    strike: float
    right: Literal["C", "P"]
    bid: float
    ask: float
    last: float
    iv: float
    delta: float
    gamma: float
    open_interest: float
    volume: float = 0.0
    iv_percentile: float | None = None
    quote_time: datetime | None = None
    multiplier: int = 100
    vega: float | None = None
    theta: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.ask > 0 and self.bid >= 0 and self.ask >= self.bid else self.last

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 and self.ask >= self.bid else float("inf")


@dataclass(frozen=True)
class ShortSnapshot:
    day: date
    short_interest: float | None = None
    short_interest_5d_ago: float | None = None
    short_volume_ratio: float | None = None
    short_volume_ratio_5d_ago: float | None = None
    short_interest_date: date | None = None
    previous_short_interest_date: date | None = None
    short_volume_ratio_20d_avg: float | None = None
    short_volume_date: date | None = None


@dataclass(frozen=True)
class FundamentalSnapshot:
    catalyst: str = ""
    catalyst_present: bool = False
    valuation_regime_break: bool = False
    valuation_reason: str = ""
    thesis_status: Literal["intact", "uncertain", "broken"] = "uncertain"
    evidence: tuple[str, ...] = ()
    confidence: float | None = None
    as_of: date | None = None
    needs_review: bool = False
    sources: tuple[str, ...] = ()
    event_class: str = "UNKNOWN"
    selloff_explanation: str = ""
    material_news_found: bool = False
    source_ids: tuple[str, ...] = ()
    news_packet_sha256: str = ""
    retrieval_mode: str = "NO_VERIFIED_RETRIEVAL"
    point_in_time_collection_ok: bool = False
    native_web_search_requested: bool = False
    native_web_search_observed: bool = False
    research_request_id: str = ""


@dataclass(frozen=True)
class OptionHistorySnapshot:
    symbol: str
    day: date
    near_atm_iv: float | None = None


@dataclass(frozen=True)
class OptionRegimeSnapshot:
    """Point-in-time underlying option statistics supplied by Moomoo.

    Percentile/rank fields are normalized to [0, 1]. IV/HV fields are
    normalized to decimal volatility. The vendor calculation methodology is
    not re-labelled as a locally reconstructed rolling ATM series.
    """

    symbol: str
    day: date
    iv: float | None = None
    iv_rank: float | None = None
    iv_percentile: float | None = None
    iv_change: float | None = None
    hv: float | None = None
    hv_change: float | None = None
    put_call_volume_ratio: float | None = None
    put_call_open_interest_ratio: float | None = None
    source: str = "moomoo_option_underlying_rank"


@dataclass(frozen=True)
class Candidate:
    symbol: str
    as_of: date
    screener_reason: str = ""
    screener_values: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Position:
    symbol: str
    option_symbol: str
    entry_date: date
    entry_price: float
    entry_spot: float
    contracts: int
    risk_per_contract: float
    expiry: date
    thesis: str = ""
    current_option_price: float | None = None
    entry_atr: float | None = None
    hard_stop_spot: float | None = None
    option_stop_price: float | None = None
    target_spot: float | None = None
    target_option_price: float | None = None
    target_source: str = ""
    entry_iv: float | None = None
    entry_iv_percentile: float | None = None
    max_hold_days: int = 30
    max_option_price: float | None = None
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    multiplier: int = 100
    currency: Literal["USD"] = "USD"
    # Planned stop loss remains the R-multiple denominator.  A long option's
    # full-premium tail capital risk is the paid premium plus entry costs
    # because stops are not guaranteed through gaps or an absent bid.
    capital_at_risk_per_contract: float | None = None
    partial_exit_taken: bool = False


@dataclass(frozen=True)
class Config:
    # Thresholds are deliberately explicit. There is no weighted score in the
    # decision rule; every gate represents a separate falsifiable hypothesis.
    strategy_version: str = "0.7.0"
    screener_min_price: float = 5.0
    screener_min_market_cap: float = 1_000_000_000.0
    screener_max_5d_change: float = -0.08
    screener_min_volume_ratio: float = 2.0
    screener_min_avg_dollar_volume: float = 20_000_000.0
    screener_max_results: int = 200
    tracked_max_age_days: int = 30
    option_surface_min_open_interest: int = 10
    option_flow_min_volume: int = 10
    option_screener_max_contracts: int = 4000
    min_history_bars: int = 80
    max_bar_age_days: int = 4
    require_bar_on_as_of: bool = True
    min_drawdown_60d: float = -0.20
    min_drawdown_5d: float = -0.08
    min_volume_zscore: float = 1.5
    min_range_atr: float = 1.5
    max_close_location: float = 0.30
    min_yz_ratio: float = 1.25
    min_downside_share: float = 0.60
    min_option_stress_signals: int = 2
    min_atr_compression: float = 0.85
    min_rv_compression: float = 0.85
    min_volume_compression: float = 0.85
    min_post_shock_sessions: int = 2
    max_vacuum_overhead_share: float = 0.25
    max_vacuum_density_ratio: float = 0.75
    min_room_to_resistance_atr: float = 1.0
    # Profile/short/gamma inputs remain observable research features.  They
    # are opt-in decision rules because current evidence does not justify
    # treating them as universal reversal-alpha requirements.
    require_intraday_profile: bool = False
    require_rebound_geometry_gate: bool = False
    require_short_weakening_gate: bool = False
    veto_short_deterioration: bool = True
    allow_unsigned_gamma_target: bool = False
    # Candidate.screener_values can carry point-in-time benchmark returns.
    # Until those values are supplied, residual shocks are diagnostics only;
    # enabling either requirement deliberately fails closed on missing data.
    require_market_residual_shock: bool = False
    require_sector_residual_shock: bool = False
    max_market_residual_5d: float = -0.08
    max_sector_residual_5d: float = -0.08
    profile_value_area_pct: float = 0.70
    profile_min_bars: int = 100
    max_spread_pct: float = 0.10
    max_premium_to_spot: float = 0.15
    min_option_open_interest: int = 100
    min_option_volume: int = 10
    require_option_quote_timestamp: bool = True
    min_dte: int = 45
    max_dte: int = 150
    target_delta_low: float = 0.25
    target_delta_high: float = 0.45
    max_iv_percentile: float = 0.85
    min_iv_history_observations: int = 60
    max_iv_to_yz_ratio: float = 2.25
    require_iv_regime_gate: bool = False
    require_option_surface_gate: bool = False
    preferred_right: str = "C"
    # Conditional option Monte Carlo.  RESEARCH_ONLY records distributional
    # diagnostics but lets only probability-free target/stop economics affect
    # selection.  OOS_GATE is unavailable until the complete distribution has
    # explicit out-of-sample evidence.
    option_mc_enabled: bool = True
    option_mc_mode: str = "RESEARCH_ONLY"
    option_mc_paths: int = 5000
    option_mc_seed: int = 17
    option_mc_horizon_trading_days: int = 10
    option_mc_rebound_probability: float = 0.40
    option_mc_continuation_probability: float = 0.30
    option_mc_assumption_source: str = "UNVALIDATED_PRIOR"
    option_mc_out_of_sample_validated: bool = False
    option_mc_validation_study_path: str | None = None
    option_mc_validation_study_sha256: str | None = None
    option_mc_validation_sample_end: str | None = None
    option_mc_validation_observation_days: int = 0
    option_mc_validation_independent_events: int = 0
    option_mc_min_validation_observation_days: int = 252
    option_mc_min_validation_independent_events: int = 100
    option_mc_min_expected_return: float = 0.0
    option_mc_min_probability_profit: float = 0.50
    option_mc_min_target_scenario_return: float = 0.0
    option_mc_min_robust_score: float = 0.0
    option_mc_max_cvar_capital_fraction: float = 1.0
    option_mc_min_target_payoff_to_loss: float = 0.0
    max_25d_put_call_skew: float = 0.20
    min_term_structure_slope: float = -0.10
    max_term_structure_slope: float = 0.15
    require_position_sizing: bool = True
    stop_atr_multiple: float = 1.25
    stop_trigger_on_intraday_low: bool = True
    option_premium_stop_pct: float = 0.55
    option_fee_per_contract: float = 2.0
    take_profit_r: float = 1.5
    min_dte_exit: int = 21
    forced_expiry_exit_dte: int = 5
    max_holding_days: int = 30
    max_iv_crush_pct: float = 0.25
    profit_protect_activation_r: float = 1.0
    profit_protect_floor_r: float = 0.0
    allow_partial_exit: bool = True
    short_interest_max_age_days: int = 21
    short_volume_max_age_days: int = 4
    max_fundamental_age_days: int = 7
    min_fundamental_confidence: float = 0.65
    max_profile_distance_atr: float = 3.0
    gamma_wall_min_abs_gex_share: float = 0.05
    target_horizon_days: int = 15
    target_iv_crush_pct: float = 0.15
    stop_iv_change_pct: float = 0.05
    risk_free_rate: float = 0.04
    dividend_yield: float = 0.0
    max_intraday_calendar_days: int = 90
    max_portfolio_premium_allocation: float = 0.20
    max_portfolio_risk_allocation: float = 0.09
    max_open_positions: int = 3
    max_positions_per_underlying: int = 1


@dataclass
class Evaluation:
    symbol: str
    as_of: date
    status: Literal["BUY_CANDIDATE", "PILOT_CANDIDATE", "WATCH", "NO_TRADE"]
    hard_gates: dict[str, bool]
    metrics: dict[str, float | str | bool | None]
    selected_option: OptionQuote | None = None
    reasons: list[str] = field(default_factory=list)
    passed_gates: int = 0
