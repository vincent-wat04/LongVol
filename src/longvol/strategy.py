from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

from .config import validate_option_mc_evidence
from .data_quality import (validate_bars, validate_context, validate_options,
                           validate_profile_bars)
from .kelly import FixedRiskInput, KellyInput, size_position_input
from .metrics import (aligned_option_panic_metrics, atr, compression_metrics,
                      gamma_walls, historical_percentile, option_quality,
                      option_surface_metrics, panic_metrics,
                      residual_return_metrics, short_pressure, volume_profile)
from .models import (Bar, Candidate, Config, Evaluation, FundamentalSnapshot,
                     OptionQuote, OptionRegimeSnapshot, Position, ShortSnapshot)
from .option_simulation import (ContractFilter, EconomicPolicy, OptionSimulation,
                                SimulationSettings, build_reversal_distribution,
                                rank_option_contracts)
from .portfolio import check_portfolio_capacity
from .pricing import scenario_prices


def _selected_option_metrics(option: OptionQuote | None, as_of) -> dict[str, float | bool | None]:
    if option is None:
        return {
            "available": False, "spread_pct": None, "dte": None,
            "delta": None, "iv": None, "entry_price": None,
        }
    return {
        "available": True,
        "spread_pct": option.spread_pct,
        "dte": (option.expiry - as_of).days,
        "delta": abs(option.delta),
        "iv": option.iv,
        "iv_percentile": option.iv_percentile,
        "entry_price": option.ask,
        "option_open_interest": option.open_interest,
        "option_volume": option.volume,
    }


def _deterministic_payoff_to_loss(result: OptionSimulation) -> float | None:
    gain = result.target_scenario.pnl_per_contract
    loss = -result.stop_scenario.pnl_per_contract
    return gain / loss if gain > 0 and loss > 0 else None


def _simulation_diagnostics(result: OptionSimulation | None) -> dict[str, float | str | bool | None]:
    if result is None:
        return {
            "option_mc_expected_pnl_per_contract": None,
            "option_mc_expected_return": None,
            "option_mc_expected_r_multiple": None,
            "option_mc_probability_profit": None,
            "option_mc_var_loss_per_contract": None,
            "option_mc_cvar_loss_per_contract": None,
            "option_mc_cvar_capital_fraction": None,
            "option_mc_robust_score": None,
            "option_mc_economic_policy_passed": False,
            "option_mc_full_premium_capital_risk": None,
            "option_mc_planned_risk_per_contract": None,
            "option_mc_modeled_stop_loss_per_contract": None,
            "option_mc_planned_option_exit_bid": None,
            "option_mc_operational_premium_stop_price": None,
            "option_mc_operational_premium_stop_loss_per_contract": None,
            "option_mc_planned_risk_basis": None,
            "option_target_payoff_to_loss": None,
        }
    state_metrics: dict[str, float | str | bool | None] = {}
    for state in result.state_outcomes:
        prefix = f"option_mc_state_{state.name.lower()}"
        state_metrics.update({
            f"{prefix}_assumed_probability": state.assumed_probability,
            f"{prefix}_paths": state.paths,
            f"{prefix}_expected_return": state.expected_return,
            f"{prefix}_probability_profit": state.probability_profit,
            f"{prefix}_target_exit_probability": state.target_exit_probability,
            f"{prefix}_stop_exit_probability": state.stop_exit_probability,
        })
    return {
        "option_mc_expected_pnl_per_contract": result.expected_pnl_per_contract,
        "option_mc_expected_return": result.expected_return,
        "option_mc_expected_r_multiple": result.expected_r_multiple,
        "option_mc_median_return": result.median_return,
        "option_mc_probability_profit": result.probability_profit,
        "option_mc_var_loss_per_contract": result.var_loss_per_contract,
        "option_mc_cvar_loss_per_contract": result.cvar_loss_per_contract,
        "option_mc_var_capital_fraction": result.var_capital_fraction,
        "option_mc_cvar_capital_fraction": result.cvar_capital_fraction,
        "option_mc_p05_return": result.p05_return,
        "option_mc_p95_return": result.p95_return,
        "option_mc_robust_score": result.robust_score,
        "option_mc_target_exit_probability": result.target_exit_probability,
        "option_mc_stop_exit_probability": result.stop_exit_probability,
        "option_mc_premium_stop_exit_probability": result.premium_stop_exit_probability,
        "option_mc_time_exit_probability": result.time_exit_probability,
        "option_mc_mean_exit_day": result.mean_exit_day,
        "option_mc_full_premium_capital_risk": result.full_premium_capital_risk,
        "option_mc_planned_risk_per_contract": result.planned_risk_per_contract,
        "option_mc_modeled_stop_loss_per_contract": result.modeled_stop_loss_per_contract,
        "option_mc_planned_option_exit_bid": (
            result.stop_scenario.executable_option_price
        ),
        "option_mc_operational_premium_stop_price": (
            result.operational_premium_stop_price
        ),
        "option_mc_operational_premium_stop_loss_per_contract": (
            result.operational_premium_stop_loss_per_contract
        ),
        "option_mc_planned_risk_basis": result.planned_risk_basis,
        "option_mc_model_price_scale": result.model_price_scale,
        "option_mc_exit_haircut": result.exit_haircut,
        "option_mc_economic_policy_passed": result.passes_economic_policy,
        "option_mc_economic_reasons": " | ".join(result.economic_reasons),
        "option_mc_target_scenario_return": result.target_scenario.return_on_capital,
        "option_mc_target_scenario_r": result.target_scenario.r_multiple,
        "option_mc_stop_scenario_return": result.stop_scenario.return_on_capital,
        "option_mc_stop_scenario_r": result.stop_scenario.r_multiple,
        "option_target_payoff_to_loss": _deterministic_payoff_to_loss(result),
        **state_metrics,
    }


def _select_option_with_mc(
    options: list[OptionQuote], candidate: Candidate, close: float,
    current_atr: float, target_spot: float, hard_stop_spot: float,
    annualized_volatility: float, config: Config,
) -> tuple[OptionQuote | None, dict[str, float | bool | None],
           OptionSimulation | None, dict[str, float | str | bool | None], bool, bool]:
    """Simulate every execution-eligible quote before applying mode semantics."""
    if config.option_mc_mode not in {"RESEARCH_ONLY", "OOS_GATE"}:
        raise ValueError("option_mc_mode must be RESEARCH_ONLY or OOS_GATE")
    # Do not trust a Config-embedded boolean.  Direct API callers and loaded
    # configs go through the same file hash, manifest, specification, code,
    # and point-in-time cutoff verification.
    evidence = validate_option_mc_evidence(config, as_of=candidate.as_of)

    distribution = build_reversal_distribution(
        spot=close,
        atr=max(current_atr, 0.01),
        target_spot=target_spot,
        hard_stop_spot=hard_stop_spot,
        horizon_days=config.option_mc_horizon_trading_days,
        annualized_volatility=max(annualized_volatility, 0.01),
        rebound_probability=config.option_mc_rebound_probability,
        continuation_probability=config.option_mc_continuation_probability,
        rebound_iv_crush=config.target_iv_crush_pct,
        continuation_iv_expansion=config.stop_iv_change_pct,
        assumption_source=config.option_mc_assumption_source,
        out_of_sample_validated=config.option_mc_out_of_sample_validated,
    )
    settings = SimulationSettings(
        paths=config.option_mc_paths,
        seed=config.option_mc_seed,
        risk_free_rate=config.risk_free_rate,
        dividend_yield=config.dividend_yield,
        premium_stop_loss_fraction=config.option_premium_stop_pct,
        entry_fee_per_contract=config.option_fee_per_contract,
        exit_fee_per_contract=config.option_fee_per_contract,
    )
    contract_filter = ContractFilter(
        right=config.preferred_right,
        min_dte=config.min_dte,
        max_dte=config.max_dte,
        min_exit_dte=config.forced_expiry_exit_dte,
        min_abs_delta=config.target_delta_low,
        max_abs_delta=config.target_delta_high,
        min_open_interest=config.min_option_open_interest,
        min_volume=config.min_option_volume,
        max_spread_pct=config.max_spread_pct,
        max_premium_to_spot=config.max_premium_to_spot,
        require_quote_on_as_of=config.require_option_quote_timestamp,
    )
    if config.option_mc_mode == "OOS_GATE":
        policy = EconomicPolicy(
            min_expected_return=config.option_mc_min_expected_return,
            min_probability_profit=config.option_mc_min_probability_profit,
            min_target_scenario_return=config.option_mc_min_target_scenario_return,
            min_robust_score=config.option_mc_min_robust_score,
            max_cvar_capital_fraction=config.option_mc_max_cvar_capital_fraction,
            require_oos_distribution=True,
        )
    else:
        # Probability-bearing outputs are diagnostics only.  No MC result can
        # pass or fail the hard gate in RESEARCH_ONLY mode.
        policy = EconomicPolicy(
            min_expected_return=None,
            min_probability_profit=None,
            min_target_scenario_return=None,
            min_robust_score=None,
            max_cvar_capital_fraction=None,
            require_oos_distribution=False,
        )
    ranking = rank_option_contracts(
        options, candidate.as_of, distribution, settings, contract_filter, policy,
    )
    deterministic_positive: list[tuple[OptionSimulation, float]] = []
    selectable: list[tuple[OptionSimulation, float]] = []
    for result in ranking.ranked:
        ratio = _deterministic_payoff_to_loss(result)
        deterministic_ok = bool(
            result.target_scenario.pnl_per_contract > 0 and
            result.stop_scenario.pnl_per_contract < 0 and
            ratio is not None and ratio >= config.option_mc_min_target_payoff_to_loss
        )
        if deterministic_ok:
            item = (result, float(ratio))
            deterministic_positive.append(item)
            if config.option_mc_mode != "OOS_GATE" or result.passes_economic_policy:
                selectable.append(item)

    if config.option_mc_mode == "RESEARCH_ONLY":
        # Deterministic payoff/loss is probability-free and therefore the only
        # economic ranking input allowed before OOS validation. Spread is a
        # secondary execution-quality tie-breaker, never the alpha score.
        selectable.sort(key=lambda item: (
            -item[1], item[0].option.spread_pct,
            abs(abs(item[0].option.delta) - 0.50), item[0].option.symbol,
        ))
    selected_result = selectable[0][0] if selectable else None
    selected = selected_result.option if selected_result else None
    if config.option_mc_mode == "RESEARCH_ONLY":
        diagnostic_order = sorted(ranking.ranked, key=lambda result: (
            _deterministic_payoff_to_loss(result) is None,
            -(_deterministic_payoff_to_loss(result) or 0.0),
            result.option.spread_pct,
            result.option.symbol,
        ))
    else:
        diagnostic_order = list(ranking.ranked)
    ranking_summary = []
    for rank, result in enumerate(diagnostic_order, start=1):
        ratio = _deterministic_payoff_to_loss(result)
        ranking_summary.append({
            "rank": rank,
            "symbol": result.option.symbol,
            "target_payoff_to_loss": ratio,
            "target_price": result.target_scenario.executable_option_price,
            "stop_price": result.stop_scenario.executable_option_price,
            "expected_return": result.expected_return,
            "probability_profit": result.probability_profit,
            "cvar_capital_fraction": result.cvar_capital_fraction,
            "robust_score": result.robust_score,
            "economic_policy_passed": result.passes_economic_policy,
            "economic_reasons": list(result.economic_reasons),
        })
    rejection_summary = "; ".join(
        f"{item.symbol}:{','.join(item.reasons)}" for item in ranking.rejected
    )
    diagnostics: dict[str, float | str | bool | None] = {
        **_simulation_diagnostics(selected_result),
        "option_mc_contracts_simulated": len(ranking.ranked),
        "option_mc_contracts_rejected": len(ranking.rejected),
        "option_mc_contracts_deterministic_positive": len(deterministic_positive),
        "option_mc_contracts_policy_passed": sum(
            result.passes_economic_policy for result in ranking.ranked
        ),
        "option_mc_contracts_selectable": len(selectable),
        "option_mc_rejection_summary": rejection_summary,
        "option_mc_ranking_summary_json": json.dumps(
            ranking_summary, sort_keys=True, separators=(",", ":"),
        ),
        "option_mc_selected_rank": next(
            (item["rank"] for item in ranking_summary
             if selected is not None and item["symbol"] == selected.symbol),
            None,
        ),
        "option_mc_economic_policy_applied": config.option_mc_mode == "OOS_GATE",
        "option_mc_economic_policy_passed": (
            selected_result.passes_economic_policy
            if selected_result is not None and config.option_mc_mode == "OOS_GATE" else None
        ),
        "option_mc_selection_rule": (
            "DETERMINISTIC_TARGET_PAYOFF_TO_LOSS_THEN_SPREAD"
            if config.option_mc_mode == "RESEARCH_ONLY" else
            "OOS_TAIL_PENALIZED_ECONOMICS"
        ),
        "option_mc_evidence_spec_sha256": (
            evidence.get("spec_sha256") if evidence is not None else None
        ),
        "option_mc_evidence_implementation_sha256": (
            evidence.get("implementation_sha256") if evidence is not None else None
        ),
        "option_mc_evidence_feature_end": (
            evidence.get("feature_end") if evidence is not None else None
        ),
        "option_mc_evidence_label_end": (
            evidence.get("label_end") if evidence is not None else None
        ),
        "option_mc_evidence_approved_at": (
            evidence.get("approved_at") if evidence is not None else None
        ),
    }
    execution_available = bool(ranking.ranked)
    economics_ok = selected_result is not None
    return (selected, _selected_option_metrics(selected, candidate.as_of),
            selected_result, diagnostics, execution_available, economics_ok)


def evaluate(candidate: Candidate, bars: list[Bar], options, short: ShortSnapshot | None,
             fundamentals: FundamentalSnapshot | None, config: Config = Config(),
             sizing: KellyInput | FixedRiskInput | None = None,
             sizing_mode: str = "UNAVAILABLE",
             previous_near_atm_iv: float | None = None,
             profile_bars: list[Bar] | None = None, iv_history: list[float] | None = None,
             option_regime: OptionRegimeSnapshot | None = None,
             portfolio_positions: list[Position] | None = None,
             portfolio_limits: tuple[float, float, int, int] | None = None,
             shock_options=None,
             shock_option_day: date | None = None) -> Evaluation:
    """Apply sequential falsifiable gates; no weighted score is used."""
    if config.option_mc_mode == "OOS_GATE":
        validate_option_mc_evidence(config, as_of=candidate.as_of)
    if not bars:
        return Evaluation(candidate.symbol, candidate.as_of, "NO_TRADE", {"data": False}, {}, reasons=["missing OHLCV"])
    options = list(options or [])
    bar_quality = validate_bars(bars, candidate.as_of, config)
    option_quality_check = validate_options(options, candidate.as_of, config)
    structurally_usable_options = [o for o in options if (
        o.expiry > candidate.as_of and o.strike > 0 and o.bid >= 0 and o.ask >= o.bid
        and o.iv > 0 and -1 <= o.delta <= 1 and o.gamma >= 0
    )]
    usable_options = [o for o in structurally_usable_options if (
        not config.require_option_quote_timestamp or
        o.quote_time is not None and o.quote_time.date() == candidate.as_of
    )]
    context_quality = validate_context(short, fundamentals, candidate.as_of, config)
    if not bar_quality.ok:
        return Evaluation(candidate.symbol, candidate.as_of, "NO_TRADE", {"daily_data": False},
                          {**bar_quality.checks}, reasons=list(bar_quality.issues))
    close = bars[-1].close
    p = panic_metrics(bars, config.min_volume_zscore, config.min_range_atr, config.max_close_location)
    # Volatility/downside confirmation describes the shock, not the later
    # entry observation.  Recompute it on data that actually existed through
    # the anchored shock bar; keep the full entry-day values in ``p`` for the
    # conditional option distribution and diagnostics.
    anchored_shock_index = p.get("recent_shock_index")
    shock_context = (
        panic_metrics(
            bars[:int(anchored_shock_index) + 1], config.min_volume_zscore,
            config.min_range_atr, config.max_close_location,
        )
        if anchored_shock_index is not None else p
    )
    residual = residual_return_metrics(bars, candidate.screener_values)
    c = compression_metrics(
        bars, config.min_atr_compression, config.min_rv_compression,
        config.min_volume_compression,
        int(p["recent_shock_index"]) if p.get("recent_shock_index") is not None else None,
    )
    start = int(p["panic_start"])
    leg = bars[start:] if start else bars[-60:]
    profile_input = ([b for b in profile_bars if b.day >= leg[0].day]
                     if profile_bars else leg)
    profile_quality = validate_profile_bars(profile_input if profile_bars else None, candidate.as_of, config)
    profile = volume_profile(profile_input, value_area_pct=config.profile_value_area_pct,
                             source_quality="intraday" if profile_bars else "daily_fallback")
    short_for_metrics = short
    if short:
        if not context_quality.checks["short_interest_fresh"]:
            short_for_metrics = replace(short_for_metrics, short_interest=None, short_interest_5d_ago=None)
        if not context_quality.checks["short_flow_fresh"]:
            short_for_metrics = replace(short_for_metrics, short_volume_ratio=None,
                                        short_volume_ratio_5d_ago=None, short_volume_ratio_20d_avg=None)
    short_m = short_pressure(short_for_metrics)
    surface = option_surface_metrics(usable_options, close, candidate.as_of)
    shock_day = (
        date.fromisoformat(str(p["recent_shock_day"]))
        if p.get("recent_shock_day") else None
    )
    if isinstance(shock_option_day, str):
        shock_option_day = date.fromisoformat(shock_option_day)
    shock_index = p.get("recent_shock_index")
    shock_spot = (
        bars[int(shock_index)].close
        if shock_index is not None and 0 <= int(shock_index) < len(bars) else close
    )
    if shock_day == candidate.as_of:
        panic_options = structurally_usable_options
        panic_snapshot_day = candidate.as_of
        option_panic_source = "ENTRY_CHAIN_SAME_DAY"
    elif shock_day is not None and shock_options is not None:
        panic_options = list(shock_options)
        panic_snapshot_day = shock_option_day
        option_panic_source = "ARCHIVED_SHOCK_CHAIN"
    elif shock_day is not None:
        panic_options = []
        panic_snapshot_day = None
        option_panic_source = "MISSING_HISTORICAL_ARCHIVE"
    else:
        panic_options = []
        panic_snapshot_day = None
        option_panic_source = "NO_SHOCK_EVENT"
    option_panic = aligned_option_panic_metrics(
        panic_options, shock_spot, shock_day, panic_snapshot_day,
        previous_near_atm_iv, config.min_option_stress_signals,
        config.require_option_quote_timestamp,
    )
    shock_option_metrics = {
        (f"shock_{key}" if key.startswith("option_") else f"shock_option_{key}"): value
        for key, value in option_panic.items()
        if key not in {
            "option_panic_shock_day", "option_panic_snapshot_day",
            "option_panic_alignment_status", "option_panic_status",
            "option_panic_alignment_ok",
        }
    }
    walls = gamma_walls(usable_options, close)
    material_walls = [w for w in walls if float(w["abs_gex_share"]) >= config.gamma_wall_min_abs_gex_share]
    above = [w for w in material_walls if float(w["strike"]) > close]
    below = [w for w in material_walls if float(w["strike"]) < close]
    wall_above = min(above, key=lambda x: float(x["strike"]), default=None)
    wall_below = max(below, key=lambda x: float(x["strike"]), default=None)
    current_atr = atr(bars)
    distance_resistance = ((profile.resistance_price - close) / current_atr
                           if profile.resistance_price is not None and current_atr else None)
    distance_support = ((close - profile.support_price) / current_atr
                        if profile.support_price is not None and current_atr else None)
    gamma_support_distance = ((close - float(wall_below["strike"])) / current_atr
                              if wall_below and current_atr else None)
    target_candidates: list[tuple[float, str]] = []
    # A daily-bar profile is a diagnostic fallback, not sufficiently precise
    # to define executable option economics.  Only a quality-passed intraday
    # profile may set the target, and sub-ATR nodes cannot manufacture a tiny
    # target that makes the reversal payoff structurally unattractive.
    if profile_quality.ok:
        for level, source in (
            (profile.resistance_price, "PROFILE_HVN"),
            (profile.value_area_low, "PROFILE_VAL"),
            (profile.peak_price, "PROFILE_POC"),
        ):
            if (level is not None and level > close and current_atr > 0 and
                    (level - close) / current_atr >= config.min_room_to_resistance_atr):
                target_candidates.append((level, source))
    # Unsigned OI×gamma identifies concentration, not dealer positioning.  It
    # remains visible in diagnostics but cannot shorten the economic target
    # unless a user explicitly opts into the legacy proxy behavior.
    if wall_above and config.allow_unsigned_gamma_target:
        target_candidates.append((float(wall_above["strike"]), "GAMMA_CONCENTRATION_PROXY"))
    target_spot, target_source = min(target_candidates, default=(close + 2 * current_atr, "ATR_FALLBACK"))
    distance_target = (target_spot - close) / current_atr if current_atr else None
    hard_stop_spot = max(0.01, close - config.stop_atr_multiple * current_atr)

    drawdown_ok = bool(p["drawdown_60d"] <= config.min_drawdown_60d and p["drawdown_5d"] <= config.min_drawdown_5d)
    forced_liquidation = bool(p["recent_forced_liquidation_event"])
    price_flow_gate = bool(drawdown_ok and p["recent_shock_event"])
    market_residual_observed = bool(
        residual["market_residual_return_5d"] is not None and
        float(residual["market_residual_return_5d"]) <= config.max_market_residual_5d
    )
    sector_residual_observed = bool(
        residual["sector_residual_return_5d"] is not None and
        float(residual["sector_residual_return_5d"]) <= config.max_sector_residual_5d
    )
    market_residual_gate = (
        market_residual_observed if config.require_market_residual_shock else True
    )
    sector_residual_gate = (
        sector_residual_observed if config.require_sector_residual_shock else True
    )
    downside_vol_gate = bool(
        shock_context["yang_zhang_ratio"] >= config.min_yz_ratio and
        shock_context["downside_return_share"] >= config.min_downside_share
    )
    option_panic_gate = bool(option_panic["option_panic_gate"])
    panic_signal_count = int(price_flow_gate) + int(downside_vol_gate) + int(option_panic_gate)
    panic_confirmed = bool(price_flow_gate and panic_signal_count >= 2)

    sessions_since_shock = p["sessions_since_recent_shock"]
    post_panic_sequence_ready = bool(
        panic_confirmed and sessions_since_shock is not None and
        int(sessions_since_shock) >= config.min_post_shock_sessions
    )
    raw_compressed = bool(c["raw_compressed"])
    post_panic_compressed = bool(post_panic_sequence_ready and raw_compressed)
    c = {
        **c,
        "compressed": post_panic_compressed,
        "post_panic_sequence_ready": post_panic_sequence_ready,
        "min_post_shock_sessions": config.min_post_shock_sessions,
    }
    reversal = bool(close > (bars[-2].close if len(bars) > 1 else close) and
                    close_location(bars[-1]) >= 0.65)
    not_new_low = bool(len(bars) < 3 or bars[-1].low >= min(b.low for b in bars[-3:-1]))
    stabilization_gate = bool(c["compressed"] and reversal and not_new_low)
    profile_data_observed = bool(profile_quality.ok)
    profile_source_gate = profile_data_observed if config.require_intraday_profile else True
    vacuum_gate = bool(profile.overhead_share_10pct <= config.max_vacuum_overhead_share and
                       profile.overhead_density_ratio <= config.max_vacuum_density_ratio and
                       (distance_resistance is None or distance_resistance >= config.min_room_to_resistance_atr) and
                       distance_target is not None and distance_target >= config.min_room_to_resistance_atr)
    # Gamma concentration is not counted as support because ordinary OI does
    # not reveal the dealer's side. It may still be used as a target/obstacle.
    support_gate = bool(distance_support is not None and 0 <= distance_support <= config.max_profile_distance_atr)
    rebound_geometry_observed = bool(vacuum_gate and support_gate)
    geometry_gate = (
        bool(profile_source_gate and rebound_geometry_observed)
        if config.require_rebound_geometry_gate else True
    )
    iv_history = iv_history or []
    current_iv_percentile = historical_percentile(
        float(surface["near_atm_iv"]) if surface["near_atm_iv"] is not None else None,
        iv_history,
    )
    iv_to_yz = (float(surface["near_atm_iv"]) / float(p["yang_zhang_vol"])
                if surface["near_atm_iv"] is not None and float(p["yang_zhang_vol"]) > 0 else None)
    surface_structure_ok = bool(
        surface["surface_available"] and surface["put_call_25d_skew"] is not None and
        surface["term_structure_slope"] is not None and
        float(surface["put_call_25d_skew"]) <= config.max_25d_put_call_skew and
        config.min_term_structure_slope <= float(surface["term_structure_slope"]) <= config.max_term_structure_slope and
        iv_to_yz is not None and iv_to_yz <= config.max_iv_to_yz_ratio
    )
    local_iv_ready = bool(
        len(iv_history) >= config.min_iv_history_observations and
        current_iv_percentile is not None and current_iv_percentile <= config.max_iv_percentile
    )
    moomoo_iv_fresh = bool(option_regime and option_regime.day == candidate.as_of)
    moomoo_iv_percentile = option_regime.iv_percentile if option_regime else None
    moomoo_iv_ready = bool(
        moomoo_iv_fresh and moomoo_iv_percentile is not None and
        0 <= moomoo_iv_percentile <= config.max_iv_percentile
    )
    if sizing_mode == "COLD_START_FIXED_RISK":
        iv_regime_ok = moomoo_iv_ready
        active_iv_percentile = moomoo_iv_percentile
        iv_gate_source = "moomoo_option_underlying_rank"
    elif sizing_mode == "VALIDATED_KELLY":
        iv_regime_ok = local_iv_ready
        active_iv_percentile = current_iv_percentile
        iv_gate_source = "local_rolling_near_atm"
    else:
        iv_regime_ok = False
        active_iv_percentile = None
        iv_gate_source = "unresolved"
    iv_regime_gate = iv_regime_ok if config.require_iv_regime_gate else True
    option_surface_gate = (
        surface_structure_ok if config.require_option_surface_gate else True
    )
    conditional_stock_volatility = float(p["yang_zhang_vol"])
    conditional_stock_volatility_source = "yang_zhang_20d"
    if conditional_stock_volatility <= 0:
        conditional_stock_volatility = max(current_atr / close * (252 ** .5), .01)
        conditional_stock_volatility_source = "atr_annualized_fallback"
    selected_simulation: OptionSimulation | None = None
    mc_diagnostics: dict[str, float | str | bool | None] = {}
    scenario_target_price = None
    scenario_stop_price = None
    scenario_target_iv = None
    scenario_stop_iv = None
    scenario_horizon_days = None
    if config.option_mc_enabled:
        (selected, opt_m, selected_simulation, mc_diagnostics,
         option_tradeable, option_economics) = _select_option_with_mc(
            usable_options, candidate, close, current_atr, target_spot,
            hard_stop_spot, conditional_stock_volatility, config,
        )
        if selected_simulation:
            scenario_target_price = selected_simulation.target_scenario.executable_option_price
            scenario_stop_price = selected_simulation.stop_scenario.executable_option_price
            scenario_target_iv = selected_simulation.target_scenario.iv
            scenario_stop_iv = selected_simulation.stop_scenario.iv
            scenario_horizon_days = selected_simulation.target_scenario.day
    else:
        selected, opt_m = option_quality(usable_options, candidate.as_of, config)
        option_tradeable = bool(
            opt_m["available"] and float(opt_m["spread_pct"]) <= config.max_spread_pct and
            float(opt_m["entry_price"]) / close <= config.max_premium_to_spot
        )
        if selected:
            legacy_scenario = scenario_prices(
                selected, close, target_spot, hard_stop_spot, int(opt_m["dte"]), config,
            )
            scenario_target_price = legacy_scenario.target_option_price
            scenario_stop_price = legacy_scenario.stop_option_price
            scenario_target_iv = legacy_scenario.target_iv
            scenario_stop_iv = legacy_scenario.stop_iv
            scenario_horizon_days = legacy_scenario.horizon_days
        deterministic_gain = (
            ((float(scenario_target_price) - selected.ask) * selected.multiplier
             - 2 * config.option_fee_per_contract)
            if selected is not None and scenario_target_price is not None else 0.0
        )
        deterministic_loss = (
            ((selected.ask - float(scenario_stop_price)) * selected.multiplier
             + 2 * config.option_fee_per_contract)
            if selected is not None and scenario_stop_price is not None else 0.0
        )
        deterministic_ratio = (
            deterministic_gain / deterministic_loss
            if deterministic_gain > 0 and deterministic_loss > 0 else None
        )
        option_economics = bool(
            option_tradeable and deterministic_ratio is not None and
            deterministic_ratio >= config.option_mc_min_target_payoff_to_loss
        )
        mc_diagnostics = {
            **_simulation_diagnostics(None),
            "option_mc_contracts_simulated": 0,
            "option_mc_contracts_rejected": 0,
            "option_mc_contracts_deterministic_positive": int(option_economics),
            "option_mc_selection_rule": "DISABLED_LEGACY_SCENARIO",
            "option_target_payoff_to_loss": deterministic_ratio,
        }
    valuation_sane = bool(fundamentals and not fundamentals.valuation_regime_break)
    thesis_not_broken = bool(fundamentals and fundamentals.thesis_status != "broken")
    thesis_intact = bool(fundamentals and fundamentals.thesis_status == "intact")
    temporary_dislocation = bool(
        fundamentals and
        fundamentals.event_class == "LIQUIDITY_OR_TECHNICAL_DISLOCATION"
    )
    fundamental_pit_evidence = bool(
        fundamentals and fundamentals.point_in_time_collection_ok and
        fundamentals.news_packet_sha256 and fundamentals.source_ids and
        fundamentals.sources and fundamentals.evidence
    )
    fundamental_gate = bool(context_quality.checks["fundamental_fresh"] and
                            context_quality.checks["fundamental_reviewed"] and
                            valuation_sane and thesis_intact and
                            temporary_dislocation and fundamental_pit_evidence)
    short_weakening_observed = bool(
        context_quality.checks["short_data_fresh"] and short_m["weakening"]
    )
    short_deterioration_observed = bool(
        context_quality.checks["short_data_fresh"] and short_m["worsening"]
    )
    short_gate = (
        short_weakening_observed if config.require_short_weakening_gate else True
    )
    short_not_worsening_gate = (
        not short_deterioration_observed if config.veto_short_deterioration else True
    )
    sizing_result = size_position_input(sizing) if sizing is not None and selected is not None else None
    sizing_ready = bool(sizing_result and sizing_result.eligible)
    risk_gate = sizing_ready if config.require_position_sizing else True
    limits = portfolio_limits or (
        config.max_portfolio_premium_allocation,
        config.max_portfolio_risk_allocation,
        config.max_open_positions,
        config.max_positions_per_underlying,
    )
    portfolio = (check_portfolio_capacity(
        portfolio_positions or [], candidate.symbol, sizing.equity,
        sizing_result.capital_required if sizing_result else 0.0,
        (sizing_result.risk_per_contract * sizing_result.contracts) if sizing_result else 0.0,
        *limits,
    ) if sizing is not None and sizing_result is not None else None)
    portfolio_gate = bool(portfolio and portfolio.eligible) if config.require_position_sizing else True
    gates = {
        "daily_data": bar_quality.ok,
        "option_data": option_quality_check.ok,
        "profile_data": profile_source_gate,
        "price_flow": price_flow_gate,
        "market_residual_shock": market_residual_gate,
        "sector_residual_shock": sector_residual_gate,
        "panic_confirmed": panic_confirmed,
        "stabilization": stabilization_gate,
        "rebound_geometry": geometry_gate,
        "short_weakening": short_gate,
        "short_not_worsening": short_not_worsening_gate,
        "fundamental_review": fundamental_gate,
        "fundamental_temporary_dislocation": temporary_dislocation,
        "fundamental_pit_evidence": fundamental_pit_evidence,
        "valuation_sane": valuation_sane,
        "thesis_not_broken": thesis_not_broken,
        "thesis_intact": thesis_intact,
        "iv_regime": iv_regime_gate,
        "option_surface": option_surface_gate,
        "option_tradeable": option_tradeable,
        "option_economics": option_economics,
        "position_sizing": risk_gate,
        "portfolio_risk": portfolio_gate,
    }
    if not panic_confirmed or (fundamentals is not None and (not valuation_sane or not thesis_not_broken)):
        status = "NO_TRADE"
    elif all(gates.values()):
        status = ("PILOT_CANDIDATE" if sizing_mode == "COLD_START_FIXED_RISK"
                  else "BUY_CANDIDATE")
    else:
        status = "WATCH"
    reasons: list[str] = []
    messages = {
        "daily_data": "daily OHLCV failed quality checks", "option_data": "option chain missing or invalid",
        "profile_data": "intraday volume-profile data missing or insufficient",
        "price_flow": "price/volume dislocation gate failed", "panic_confirmed": "panic lacks a second independent volatility/options signature",
        "market_residual_shock": "market-residual dislocation is missing or below the configured magnitude",
        "sector_residual_shock": "sector-residual dislocation is missing or below the configured magnitude",
        "stabilization": "post-panic volume/volatility compression plus reversal not confirmed", "rebound_geometry": "vacuum/nearby support geometry failed",
        "short_weakening": "short pressure is not confirmed to be weakening",
        "short_not_worsening": "fresh short-flow/interest diagnostics show material deterioration",
        "valuation_sane": "possible valuation-regime break",
        "fundamental_review": "fundamental review is missing, stale, low-confidence, or not explicitly intact",
        "fundamental_temporary_dislocation": "fundamental event is not classified as a temporary liquidity/technical dislocation",
        "fundamental_pit_evidence": "fundamental classification lacks complete point-in-time packet evidence",
        "thesis_not_broken": "fundamental thesis marked broken",
        "thesis_intact": "fundamental thesis is not explicitly intact",
        "iv_regime": "active IV regime source is missing, stale, or above the configured percentile",
        "option_surface": "option skew/term structure/IV filter failed",
        "option_tradeable": "option premium or spread filter failed",
        "option_economics": "no eligible option has positive deterministic target payoff and a loss-making stop scenario",
        "position_sizing": "position sizing inputs are missing or whole-contract caps permit no position",
        "portfolio_risk": "portfolio premium/position capacity gate failed",
    }
    reasons.extend(messages[k] for k, ok in gates.items() if not ok)
    metrics = {
        **p, **c,
        "strategy_horizon": "SHORT_TERM_REVERSAL",
        "strategy_horizon_trading_days": config.option_mc_horizon_trading_days,
        "alpha_source": "UNDERLYING_LIQUIDITY_DISLOCATION_REVERSAL",
        "instrument_exposure": (
            "LONG_CALL_LONG_DELTA_LONG_GAMMA_LONG_VEGA_NEGATIVE_THETA"
            if config.preferred_right == "C" else
            "LONG_PUT_SHORT_DELTA_LONG_GAMMA_LONG_VEGA_NEGATIVE_THETA"
        ),
        "option_mc_enabled": config.option_mc_enabled,
        "option_mc_mode": config.option_mc_mode,
        "option_mc_role": (
            "DISABLED_LEGACY_SCENARIO"
            if not config.option_mc_enabled else
            "DIAGNOSTICS_ONLY_PROBABILITY_FREE_DETERMINISTIC_GATE"
            if config.option_mc_mode == "RESEARCH_ONLY" else
            "OOS_VALIDATED_ECONOMIC_GATE"
        ),
        "option_fee_per_contract_per_side": config.option_fee_per_contract,
        "option_mc_paths": config.option_mc_paths,
        "option_mc_seed": config.option_mc_seed,
        "option_mc_horizon_trading_days": config.option_mc_horizon_trading_days,
        "option_mc_rebound_probability_assumption": config.option_mc_rebound_probability,
        "option_mc_continuation_probability_assumption": config.option_mc_continuation_probability,
        "option_mc_stabilization_probability_assumption": (
            1 - config.option_mc_rebound_probability - config.option_mc_continuation_probability
        ),
        "option_mc_assumption_source": config.option_mc_assumption_source,
        "option_mc_out_of_sample_validated": config.option_mc_out_of_sample_validated,
        "option_mc_conditional_stock_volatility": conditional_stock_volatility,
        "option_mc_conditional_stock_volatility_source": conditional_stock_volatility_source,
        **mc_diagnostics,
        "current_price": close,
        "atr": current_atr,
        "panic_signal_count": panic_signal_count,
        "forced_liquidation_proxy": forced_liquidation,
        "shock_yang_zhang_vol": shock_context.get("yang_zhang_vol"),
        "shock_yang_zhang_ratio": shock_context.get("yang_zhang_ratio"),
        "shock_downside_return_share": shock_context.get("downside_return_share"),
        "shock_downside_vol_confirmation": downside_vol_gate,
        **residual,
        "market_residual_shock_observed": market_residual_observed,
        "sector_residual_shock_observed": sector_residual_observed,
        "market_residual_shock_required": config.require_market_residual_shock,
        "sector_residual_shock_required": config.require_sector_residual_shock,
        "max_market_residual_5d": config.max_market_residual_5d,
        "max_sector_residual_5d": config.max_sector_residual_5d,
        "profile_peak": profile.peak_price,
        "profile_source_quality": profile.source_quality,
        "profile_data_observed": profile_data_observed,
        "rebound_geometry_observed": rebound_geometry_observed,
        "rebound_geometry_required": config.require_rebound_geometry_gate,
        "vacuum_geometry_observed": vacuum_gate,
        "profile_support_observed": support_gate,
        "profile_value_area_low": profile.value_area_low,
        "profile_value_area_high": profile.value_area_high,
        "profile_hvn_count": len(profile.high_volume_nodes),
        "overhead_share_10pct": profile.overhead_share_10pct,
        "overhead_density_ratio": profile.overhead_density_ratio,
        "support_share_10pct": profile.support_share_10pct,
        "distance_to_profile_peak_atr": abs(profile.peak_price - close) / current_atr if current_atr else None,
        "distance_to_resistance_atr": distance_resistance, "distance_to_support_atr": distance_support,
        "gamma_support_distance_atr": gamma_support_distance,
        "nearest_gamma_wall_above": float(wall_above["strike"]) if wall_above else None,
        "nearest_gamma_wall_below": float(wall_below["strike"]) if wall_below else None,
        "nearest_gamma_wall_above_share": float(wall_above["abs_gex_share"]) if wall_above else None,
        "nearest_gamma_wall_below_share": float(wall_below["abs_gex_share"]) if wall_below else None,
        "gamma_wall_quality": "oi_gamma_proxy_screened_union_dealer_side_unknown",
        "unsigned_gamma_target_enabled": config.allow_unsigned_gamma_target,
        "entry_hard_stop_spot": hard_stop_spot,
        "target_spot": target_spot,
        "target_source": target_source,
        "max_holding_days": config.max_holding_days,
        "distance_to_target_atr": distance_target,
        "local_iv_percentile": current_iv_percentile,
        "active_iv_percentile": active_iv_percentile,
        "iv_gate_source": iv_gate_source,
        "iv_regime_observed": iv_regime_ok,
        "iv_regime_required": config.require_iv_regime_gate,
        "option_surface_observed": surface_structure_ok,
        "option_surface_required": config.require_option_surface_gate,
        "moomoo_iv_percentile": moomoo_iv_percentile,
        "moomoo_iv_rank": option_regime.iv_rank if option_regime else None,
        "moomoo_iv": option_regime.iv if option_regime else None,
        "moomoo_hv": option_regime.hv if option_regime else None,
        "moomoo_iv_change": option_regime.iv_change if option_regime else None,
        "moomoo_option_regime_day": option_regime.day.isoformat() if option_regime else None,
        "moomoo_option_regime_fresh": moomoo_iv_fresh,
        "local_iv_ready": local_iv_ready,
        "iv_history_observations": len(iv_history),
        "sizing_mode": sizing_mode,
        "iv_to_yang_zhang_ratio": iv_to_yz,
        "option_screen_union_contracts": len(usable_options),
        "option_screen_surface_oi_floor": config.option_surface_min_open_interest,
        "option_screen_flow_volume_floor": config.option_flow_min_volume,
        **bar_quality.checks, **option_quality_check.checks, **profile_quality.checks, **context_quality.checks,
        **short_m, **opt_m,
        # Preserve the historical unprefixed panic fields, but let the entry
        # surface win for overlapping surface names. Shock-day raw values are
        # also emitted under explicit ``shock_option_*`` names.
        **option_panic, **surface, **shock_option_metrics,
        "entry_option_snapshot_day": candidate.as_of.isoformat(),
        "entry_option_surface_status": (
            "KNOWN" if surface["surface_available"] else "UNKNOWN"
        ),
        "shock_option_snapshot_day": option_panic["option_panic_snapshot_day"],
        "shock_option_alignment_status": option_panic["option_panic_alignment_status"],
        "shock_option_alignment_ok": option_panic["option_panic_alignment_ok"],
        "shock_option_panic_status": option_panic["option_panic_status"],
        "shock_option_panic_source": option_panic_source,
        "short_weakening_observed": short_weakening_observed,
        "short_deterioration_observed": short_deterioration_observed,
        "short_weakening_required": config.require_short_weakening_gate,
        "short_deterioration_veto_enabled": config.veto_short_deterioration,
        "reversal": reversal,
        "not_new_low": not_new_low,
        "catalyst": fundamentals.catalyst if fundamentals else "",
        "valuation_regime_break": fundamentals.valuation_regime_break if fundamentals else None,
        "fundamental_thesis_status": fundamentals.thesis_status if fundamentals else None,
        "fundamental_thesis_intact": thesis_intact,
        "fundamental_event_class": fundamentals.event_class if fundamentals else None,
        "fundamental_temporary_dislocation": temporary_dislocation,
        "fundamental_selloff_explanation": (
            fundamentals.selloff_explanation if fundamentals else ""
        ),
        "fundamental_material_news_found": (
            fundamentals.material_news_found if fundamentals else None
        ),
        "fundamental_source_ids": (
            " | ".join(fundamentals.source_ids) if fundamentals else ""
        ),
        "fundamental_sources": (
            " | ".join(fundamentals.sources) if fundamentals else ""
        ),
        "fundamental_news_packet_sha256": (
            fundamentals.news_packet_sha256 if fundamentals else ""
        ),
        "fundamental_retrieval_mode": (
            fundamentals.retrieval_mode if fundamentals else "NO_VERIFIED_RETRIEVAL"
        ),
        "fundamental_point_in_time_collection_ok": (
            fundamentals.point_in_time_collection_ok if fundamentals else False
        ),
        "fundamental_native_web_search_requested": (
            fundamentals.native_web_search_requested if fundamentals else False
        ),
        "fundamental_native_web_search_observed": (
            fundamentals.native_web_search_observed if fundamentals else False
        ),
        "fundamental_research_request_id": (
            fundamentals.research_request_id if fundamentals else ""
        ),
    }
    if selected:
        metrics["selected_option_ask"] = selected.ask
        metrics["selected_option_mid"] = selected.mid
        metrics["selected_option_bid"] = selected.bid
        metrics["selected_option_expiry"] = selected.expiry.isoformat()
        metrics["selected_option_strike"] = selected.strike
        metrics["selected_option_right"] = selected.right
        metrics["selected_option_iv"] = selected.iv
        metrics["selected_option_delta"] = abs(selected.delta)
        metrics["selected_option_multiplier"] = selected.multiplier
        metrics.update({
            "scenario_target_option_price": scenario_target_price,
            "scenario_stop_option_price": scenario_stop_price,
            "scenario_target_iv": scenario_target_iv,
            "scenario_stop_iv": scenario_stop_iv,
            "scenario_horizon_days": scenario_horizon_days,
            "scenario_horizon_unit": (
                "TRADING_DAYS" if config.option_mc_enabled else "LEGACY_CALENDAR_DAY_APPROXIMATION"
            ),
        })
    if sizing_result:
        metrics.update({
            "position_sizing_mode": sizing_result.mode,
            "position_risk_fraction": sizing_result.applied_fraction,
            "position_contracts": sizing_result.contracts,
            "position_capital_required": sizing_result.capital_required,
            "position_risk_per_contract": sizing_result.risk_per_contract,
            "position_capital_at_risk_per_contract": sizing_result.capital_at_risk_per_contract,
            "position_premium_fraction": sizing_result.premium_fraction,
            "position_sizing_sequence": "SELECT_CONTRACT_THEN_FULL_PREMIUM_AND_PLANNED_RISK_CAPS",
            "position_sizing_reason": sizing_result.reason,
            "kelly_raw": sizing_result.raw_kelly if sizing_result.mode == "VALIDATED_KELLY" else None,
            "kelly_edge": sizing_result.edge if sizing_result.mode == "VALIDATED_KELLY" else None,
        })
    if portfolio:
        metrics.update({"portfolio_open_positions": portfolio.open_positions,
                        "portfolio_committed_premium": portfolio.committed_premium,
                        "portfolio_proposed_premium": portfolio.proposed_premium,
                        "portfolio_committed_full_premium_tail_risk": portfolio.committed_full_premium_tail_risk,
                        "portfolio_proposed_full_premium_tail_risk": portfolio.proposed_full_premium_tail_risk,
                        "portfolio_resulting_premium_fraction": portfolio.resulting_premium_fraction,
                        "portfolio_committed_risk": portfolio.committed_risk,
                        "portfolio_proposed_risk": portfolio.proposed_risk,
                        "portfolio_committed_planned_risk": portfolio.committed_planned_risk,
                        "portfolio_proposed_planned_risk": portfolio.proposed_planned_risk,
                        "portfolio_resulting_risk_fraction": portfolio.resulting_risk_fraction,
                        "portfolio_capacity_reason": portfolio.reason})
    return Evaluation(candidate.symbol, candidate.as_of, status, gates, metrics, selected, reasons, sum(gates.values()))


def close_location(bar: Bar) -> float:
    span = bar.high - bar.low
    return (bar.close - bar.low) / span if span else 0.5
