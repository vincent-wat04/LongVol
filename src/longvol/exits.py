from __future__ import annotations

from datetime import date

from .metrics import atr
from .models import Bar, Config, Evaluation, OptionQuote, Position


def _entry_atr(position: Position, bars: list[Bar]) -> float | None:
    if position.entry_atr and position.entry_atr > 0:
        return position.entry_atr
    entry_bars = [b for b in bars if b.day <= position.entry_date]
    value = atr(entry_bars) if len(entry_bars) >= 15 else 0.0
    return value or None


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def evaluate_exit(position: Position, bars: list[Bar], evaluation: Evaluation,
                  current_option: OptionQuote | None = None,
                  config: Config = Config(), as_of: date | None = None) -> dict:
    """Evaluate a long-option position using immutable entry-time levels.

    This returns an instruction proposal, never an assumed execution fill.
    The underlying stop is based on entry ATR and cannot loosen when current
    volatility changes.
    """
    base = {"symbol": position.symbol, "option_symbol": position.option_symbol}
    if not bars:
        return {**base, "action": "DATA_NEEDED", "reason": "MISSING_OHLCV", "triggers": "MISSING_OHLCV"}
    as_of = as_of or bars[-1].day
    spot = bars[-1].close
    dte = (position.expiry - as_of).days
    holding_days = (as_of - position.entry_date).days
    entry_atr = _entry_atr(position, bars)
    hard_stop = position.hard_stop_spot
    if hard_stop is None and entry_atr is not None:
        hard_stop = position.entry_spot - config.stop_atr_multiple * entry_atr

    quote_fresh = False
    if current_option is not None:
        # A long position is liquidated into the bid; do not mark exits at an
        # optimistic midpoint or a stale last trade.
        quote_fresh = (not config.require_option_quote_timestamp or
                       current_option.quote_time is not None and
                       current_option.quote_time.date() == as_of)
        current_price = current_option.bid if current_option.bid > 0 and quote_fresh else None
        current_iv = current_option.iv if current_option.iv > 0 and quote_fresh else None
        spread_pct = current_option.spread_pct
    else:
        # A value persisted on the position is not a current executable quote.
        current_price = (position.current_option_price
                         if not config.require_option_quote_timestamp else None)
        current_iv = (_num(evaluation.metrics.get("selected_option_iv") or evaluation.metrics.get("iv"))
                      if not config.require_option_quote_timestamp else None)
        spread_pct = _num(evaluation.metrics.get("spread_pct"))

    option_stop = position.option_stop_price
    if option_stop is None:
        option_stop = position.entry_price * (1 - config.option_premium_stop_pct)
    r_multiple = None
    max_r = None
    round_trip_fees = 2 * config.option_fee_per_contract
    if current_price is not None and position.risk_per_contract > 0:
        pnl_per_contract = ((float(current_price) - position.entry_price) *
                            position.multiplier - round_trip_fees)
        r_multiple = pnl_per_contract / position.risk_per_contract
        max_price = max(position.max_option_price or position.entry_price, float(current_price))
        max_pnl_per_contract = ((max_price - position.entry_price) *
                                position.multiplier - round_trip_fees)
        max_r = max_pnl_per_contract / position.risk_per_contract

    thesis_broken = (evaluation.metrics.get("valuation_regime_break") is True or
                     not evaluation.hard_gates.get("thesis_not_broken", True))
    short_worsening = evaluation.metrics.get("worsening") is True
    target_reached = bool(position.target_spot is not None and spot >= float(position.target_spot))
    option_target_reached = bool(current_price is not None and position.target_option_price is not None and
                                 float(current_price) >= position.target_option_price)
    iv_crush = bool(position.entry_iv and current_iv is not None and
                    current_iv <= position.entry_iv * (1 - config.max_iv_crush_pct))
    # Match the execution monitor's default risk-exit threshold.  A breached
    # stop with a wider market is an intervention alert, never a silent HOLD.
    premium_stop_reliable = spread_pct is None or spread_pct <= .30

    triggers: list[str] = []
    if dte <= config.forced_expiry_exit_dte:
        triggers.append("EXPIRY_RISK")
    if thesis_broken:
        triggers.append("THESIS_BREAK")
    underlying_stop_hit = bool(hard_stop is not None and
                               (bars[-1].low <= float(hard_stop)
                                if config.stop_trigger_on_intraday_low else spot <= float(hard_stop)))
    if underlying_stop_hit:
        triggers.append("UNDERLYING_HARD_STOP")
    premium_stop_hit = bool(
        current_price is not None and float(current_price) <= option_stop
    )
    if premium_stop_hit:
        triggers.append("OPTION_PREMIUM_STOP" if premium_stop_reliable
                        else "OPTION_PREMIUM_STOP_WIDE_SPREAD")
    if holding_days >= min(position.max_hold_days, config.max_holding_days):
        triggers.append("TIME_STOP")
    if dte <= config.min_dte_exit:
        triggers.append("DTE_EXIT")
    if iv_crush and (r_multiple is None or r_multiple < config.take_profit_r):
        triggers.append("IV_CRUSH")
    if short_worsening and spot < position.entry_spot:
        triggers.append("SHORT_PRESSURE_REACCELERATION")
    # A structural target may fund one trim.  Once that fill has been
    # materialized, the same persistent spot condition cannot repeatedly
    # reduce the runner.  The independent option/R target may still close it.
    if target_reached and not position.partial_exit_taken:
        triggers.append("STRUCTURE_TARGET_REACHED")
    if option_target_reached or (r_multiple is not None and r_multiple >= config.take_profit_r):
        triggers.append("OPTION_TARGET_REACHED")
    if (max_r is not None and r_multiple is not None and
            max_r >= config.profit_protect_activation_r and r_multiple <= config.profit_protect_floor_r):
        triggers.append("PROFIT_GIVEBACK")

    priority = ["EXPIRY_RISK", "THESIS_BREAK", "UNDERLYING_HARD_STOP",
                "OPTION_PREMIUM_STOP_WIDE_SPREAD", "OPTION_PREMIUM_STOP",
                "PROFIT_GIVEBACK", "TIME_STOP", "DTE_EXIT", "IV_CRUSH",
                "SHORT_PRESSURE_REACCELERATION", "STRUCTURE_TARGET_REACHED", "OPTION_TARGET_REACHED"]
    reason = next((x for x in priority if x in triggers), "NO_EXIT_TRIGGER")

    missing = []
    bar_age = (as_of - bars[-1].day).days
    if bar_age < 0:
        missing.append("FUTURE_OHLCV")
    elif bar_age > config.max_bar_age_days:
        missing.append("STALE_OHLCV")
    if current_price is None:
        missing.append("CURRENT_OPTION_BID")
    if current_option is not None and not quote_fresh:
        missing.append("STALE_OPTION_QUOTE")
    if hard_stop is None:
        missing.append("FROZEN_ENTRY_STOP")
    if position.risk_per_contract <= 0:
        missing.append("INITIAL_RISK")
    if position.target_spot is None:
        missing.append("FROZEN_TARGET_SPOT")
    if evaluation.metrics.get("exit_context_present") is False:
        missing.append("CURRENT_SIGNAL_CONTEXT")

    # Missing ancillary data must never hide a safety exit that can already be
    # established from independent inputs (expiry, thesis, underlying stop,
    # time/DTE). If no exit is established, missing inputs are an alert, not HOLD.
    if reason == "NO_EXIT_TRIGGER" and missing:
        return {**base, "action": "DATA_NEEDED", "reason": "MISSING_EXIT_INPUTS",
                "triggers": "", "missing_inputs": "|".join(missing), "spot": spot,
                "dte": dte, "holding_days": holding_days,
                "execution_price_guaranteed": False}
    risk_reasons = {
        "EXPIRY_RISK", "THESIS_BREAK", "UNDERLYING_HARD_STOP",
        "OPTION_PREMIUM_STOP", "OPTION_PREMIUM_STOP_WIDE_SPREAD",
        "PROFIT_GIVEBACK", "TIME_STOP", "DTE_EXIT", "IV_CRUSH",
        "SHORT_PRESSURE_REACCELERATION",
    }
    risk_exit = reason in risk_reasons
    if reason == "OPTION_PREMIUM_STOP_WIDE_SPREAD":
        action = "ALERT"
    elif reason in risk_reasons:
        action = "EXIT"
    elif reason == "STRUCTURE_TARGET_REACHED":
        action = ("REDUCE" if config.allow_partial_exit and
                  position.contracts > 1 and not position.partial_exit_taken
                  else "EXIT")
    elif reason == "OPTION_TARGET_REACHED":
        action = "EXIT"
    else:
        action = "HOLD"
    suggested_quantity = (position.contracts if action in {"EXIT", "ALERT"} else
                          max(1, position.contracts // 2) if action == "REDUCE" else 0)

    suggested_limit = round(float(current_price), 4) if current_price is not None else None
    return {
        **base, "action": action, "reason": reason, "triggers": "|".join(triggers),
        "spot": round(spot, 4),
        "hard_stop_spot": round(float(hard_stop), 4) if hard_stop is not None else None,
        "entry_atr": round(float(entry_atr), 4) if entry_atr is not None else None,
        "current_option_bid": round(float(current_price), 4) if current_price is not None else None,
        "option_stop_price": round(option_stop, 4),
        "target_spot": position.target_spot, "target_option_price": position.target_option_price,
        "target_source": position.target_source,
        "r_multiple": round(r_multiple, 3) if r_multiple is not None else None,
        "max_r_multiple": round(max_r, 3) if max_r is not None else None,
        "round_trip_fees_per_contract": round_trip_fees,
        "dte": dte, "holding_days": holding_days,
        "current_iv": current_iv, "entry_iv": position.entry_iv, "iv_crush": iv_crush,
        "spread_pct": spread_pct,
        "suggested_order": (
            "MANUAL_RISK_EXIT_REQUIRED" if action == "ALERT" else
            "MARKETABLE_LIMIT" if action != "HOLD" and suggested_limit is not None
            else "MANUAL_QUOTE_REQUIRED" if action != "HOLD" else "NONE"
        ),
        "suggested_limit": suggested_limit, "suggested_quantity": suggested_quantity,
        "risk_exit": risk_exit,
        "partial_exit_taken": position.partial_exit_taken,
        "execution_price_guaranteed": False,
        "missing_inputs": "|".join(missing),
        "stop_trigger_basis": "DAY_LOW" if config.stop_trigger_on_intraday_low else "CLOSE",
        "gap_below_stop": bool(hard_stop is not None and bars[-1].open <= float(hard_stop)),
    }
