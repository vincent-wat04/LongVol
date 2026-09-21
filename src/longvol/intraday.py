from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .broker import BrokerConfig, LiveQuote
from .models import Position


@dataclass(frozen=True)
class IntradayDecision:
    code: str
    action: str
    reason: str
    quantity: int = 0
    limit_price: float | None = None
    risk_exit: bool = False
    diagnostics: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _fresh(quote: LiveQuote, now: datetime, max_age_seconds: int) -> bool:
    timestamp = quote.quote_time
    if timestamp is None:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("America/New_York"))
    return 0 <= (now - timestamp.astimezone(now.tzinfo)).total_seconds() <= max_age_seconds


def _signal_bool(signal: dict, name: str) -> bool:
    value = signal.get(name)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _valid_sha256(value) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(character in "0123456789abcdefABCDEF"
                                   for character in text)


def evaluate_intraday_entry(signal: dict, underlying: LiveQuote, option: LiveQuote,
                            now: datetime, config: BrokerConfig,
                            next_eligible_session: date | None = None
                            ) -> IntradayDecision:
    now = now.astimezone(ZoneInfo("America/New_York"))
    code = str(signal.get("selected_option") or "")
    diagnostics = {"spot": underlying.last, "bid": option.bid, "ask": option.ask,
                   "spread_pct": option.spread_pct, "quote_time": str(option.quote_time)}
    if signal.get("status") not in {"BUY_CANDIDATE", "PILOT_CANDIDATE"} or not code:
        return IntradayDecision(code, "IGNORE", "NOT_ENTRY_CANDIDATE", diagnostics=diagnostics)
    fundamental_ok = bool(
        _signal_bool(signal, "fundamental_thesis_intact") and
        _signal_bool(signal, "fundamental_temporary_dislocation") and
        _signal_bool(signal, "fundamental_point_in_time_collection_ok") and
        signal.get("fundamental_event_class") ==
        "LIQUIDITY_OR_TECHNICAL_DISLOCATION" and
        _valid_sha256(signal.get("fundamental_news_packet_sha256"))
    )
    diagnostics["fundamental_signal_revalidated"] = fundamental_ok
    if not fundamental_ok:
        return IntradayDecision(code, "CANCEL", "FUNDAMENTAL_SIGNAL_INVALID",
                                diagnostics=diagnostics)
    try:
        signal_day = date.fromisoformat(str(signal["as_of"])[:10])
    except (KeyError, ValueError):
        return IntradayDecision(code, "CANCEL", "INVALID_SIGNAL_DATE", diagnostics=diagnostics)
    diagnostics["signal_session"] = signal_day.isoformat()
    diagnostics["next_eligible_session"] = (
        next_eligible_session.isoformat() if next_eligible_session else None
    )
    if next_eligible_session is None:
        return IntradayDecision(code, "WAIT", "TRADING_CALENDAR_REQUIRED",
                                diagnostics=diagnostics)
    if next_eligible_session <= signal_day:
        return IntradayDecision(code, "CANCEL", "INVALID_ELIGIBLE_SESSION",
                                diagnostics=diagnostics)
    if now.date() < next_eligible_session:
        return IntradayDecision(code, "WAIT", "BEFORE_NEXT_ELIGIBLE_SESSION",
                                diagnostics=diagnostics)
    if now.date() > next_eligible_session:
        return IntradayDecision(code, "CANCEL", "SIGNAL_EXPIRED", diagnostics=diagnostics)
    start = time.fromisoformat(config.entry_start_et)
    end = time.fromisoformat(config.entry_end_et)
    if not start <= now.time().replace(tzinfo=None) <= end:
        return IntradayDecision(code, "WAIT", "OUTSIDE_ENTRY_WINDOW", diagnostics=diagnostics)
    if not _fresh(underlying, now, config.max_quote_age_seconds) or not _fresh(option, now, config.max_quote_age_seconds):
        return IntradayDecision(code, "WAIT", "STALE_QUOTE", diagnostics=diagnostics)
    hard_stop = float(signal.get("entry_hard_stop_spot") or 0)
    reference_spot = float(signal.get("current_price") or 0)
    entry_atr = float(signal.get("atr") or 0)
    if min(hard_stop, reference_spot, entry_atr, underlying.last) <= 0:
        return IntradayDecision(code, "CANCEL", "MISSING_FROZEN_ENTRY_FIELDS", diagnostics=diagnostics)
    if underlying.last <= hard_stop:
        return IntradayDecision(code, "CANCEL", "UNDERLYING_STOP_BROKEN", diagnostics=diagnostics)
    if underlying.last > reference_spot + config.max_entry_chase_atr * entry_atr:
        return IntradayDecision(code, "CANCEL", "ENTRY_CHASE_LIMIT", diagnostics=diagnostics)
    if option.bid <= 0 or option.ask < option.bid or option.spread_pct > config.max_entry_spread_pct:
        return IntradayDecision(code, "WAIT", "ENTRY_SPREAD_TOO_WIDE", diagnostics=diagnostics)
    reference_ask = float(signal.get("selected_option_ask") or 0)
    if reference_ask <= 0:
        return IntradayDecision(code, "CANCEL", "MISSING_REFERENCE_ASK", diagnostics=diagnostics)
    if option.ask > reference_ask * (1 + config.max_entry_price_drift_pct):
        return IntradayDecision(code, "WAIT", "ENTRY_PRICE_DRIFT", diagnostics=diagnostics)
    target_option = float(signal.get("scenario_target_option_price") or 0)
    stop_option = float(signal.get("scenario_stop_option_price") or 0)
    if target_option <= option.ask or not 0 <= stop_option < option.ask:
        return IntradayDecision(code, "CANCEL", "LIVE_OPTION_SCENARIO_INVALID", diagnostics=diagnostics)
    multiplier = int(float(signal.get("selected_option_multiplier") or 100))
    round_trip_fees = 2 * config.estimated_fees_per_contract
    target_payoff = ((target_option - option.ask) * multiplier -
                     round_trip_fees)
    planned_stop_loss = ((option.ask - stop_option) * multiplier +
                         round_trip_fees)
    payoff_to_loss = (
        target_payoff / planned_stop_loss
        if target_payoff > 0 and planned_stop_loss > 0 else None
    )
    minimum_ratio = float(signal.get("minimum_target_payoff_to_loss") or 0)
    diagnostics.update({
        "round_trip_fees_per_contract": round_trip_fees,
        "live_target_payoff_per_contract": target_payoff,
        "live_planned_stop_loss_per_contract": planned_stop_loss,
        "live_target_payoff_to_loss": payoff_to_loss,
        "minimum_target_payoff_to_loss": minimum_ratio,
    })
    if target_payoff <= 0 or planned_stop_loss <= 0 or payoff_to_loss is None:
        return IntradayDecision(code, "CANCEL", "LIVE_OPTION_ECONOMICS_INVALID",
                                diagnostics=diagnostics)
    if payoff_to_loss + 1e-12 < minimum_ratio:
        return IntradayDecision(code, "CANCEL", "LIVE_PAYOFF_RATIO_BELOW_MINIMUM",
                                diagnostics=diagnostics)
    quantity = int(float(signal.get("position_contracts") or 0))
    if quantity <= 0:
        return IntradayDecision(code, "CANCEL", "NO_POSITION_QUANTITY", diagnostics=diagnostics)
    return IntradayDecision(code, "PLACE_ENTRY", "ENTRY_EXECUTION_READY", quantity,
                            round(option.ask, 3), False, diagnostics)


def evaluate_intraday_exit(position: Position, underlying: LiveQuote, option: LiveQuote,
                           now: datetime, config: BrokerConfig) -> IntradayDecision:
    now = now.astimezone(ZoneInfo("America/New_York"))
    underlying_fresh = _fresh(underlying, now, config.max_quote_age_seconds)
    option_fresh = _fresh(option, now, config.max_quote_age_seconds)
    diagnostics = {"spot": underlying.last, "bid": option.bid, "ask": option.ask,
                   "spread_pct": option.spread_pct, "quote_time": str(option.quote_time),
                   "partial_exit_taken": position.partial_exit_taken,
                   "underlying_quote_fresh": underlying_fresh,
                   "option_quote_fresh": option_fresh}
    dte = (position.expiry - now.date()).days
    holding_days = (now.date() - position.entry_date).days
    hard_reasons = []
    if dte <= config.forced_expiry_exit_dte:
        hard_reasons.append("EXPIRY_RISK")
    if (underlying_fresh and position.hard_stop_spot is not None and
            underlying.last <= position.hard_stop_spot):
        hard_reasons.append("UNDERLYING_HARD_STOP")
    if holding_days >= position.max_hold_days:
        hard_reasons.append("TIME_STOP")
    if hard_reasons:
        if not option_fresh:
            return IntradayDecision(
                position.option_symbol, "ALERT", "STALE_OPTION_QUOTE_RISK_EXIT",
                position.contracts, risk_exit=True,
                diagnostics={**diagnostics, "triggers": hard_reasons},
            )
        if option.bid <= 0 or option.ask < option.bid:
            return IntradayDecision(
                position.option_symbol, "ALERT", "NO_EXECUTABLE_BID",
                position.contracts, risk_exit=True,
                diagnostics={**diagnostics, "triggers": hard_reasons},
            )
        return IntradayDecision(position.option_symbol, "PLACE_EXIT", hard_reasons[0],
                                position.contracts, round(option.bid, 3), True,
                                {**diagnostics, "triggers": hard_reasons})
    if not underlying_fresh or not option_fresh:
        return IntradayDecision(position.option_symbol, "WAIT", "STALE_QUOTE",
                                diagnostics=diagnostics)
    if option.bid <= 0:
        return IntradayDecision(
            position.option_symbol, "ALERT", "NO_EXECUTABLE_BID",
            position.contracts, risk_exit=True, diagnostics=diagnostics,
        )
    if option.ask < option.bid:
        return IntradayDecision(position.option_symbol, "WAIT", "INVALID_OPTION_QUOTE", diagnostics=diagnostics)
    if (position.option_stop_price is not None and option.bid <= position.option_stop_price):
        if option.spread_pct > config.max_exit_spread_pct:
            return IntradayDecision(
                position.option_symbol, "ALERT", "PREMIUM_STOP_WIDE_SPREAD",
                position.contracts,
                round(option.bid, 3) if option.bid > 0 else None,
                True, diagnostics,
            )
        return IntradayDecision(position.option_symbol, "PLACE_EXIT", "OPTION_PREMIUM_STOP",
                                position.contracts, round(option.bid, 3), True, diagnostics)
    structure_target = bool(
        position.target_spot is not None and
        underlying.last >= position.target_spot
    )
    option_target = bool(
        position.target_option_price is not None and
        option.bid >= position.target_option_price
    )
    if structure_target and not position.partial_exit_taken:
        if option.spread_pct > config.max_entry_spread_pct:
            return IntradayDecision(position.option_symbol, "WAIT", "TARGET_EXIT_WIDE_SPREAD",
                                    diagnostics=diagnostics)
        quantity = position.contracts if position.contracts == 1 else max(1, position.contracts // 2)
        return IntradayDecision(position.option_symbol, "PLACE_EXIT", "STRUCTURE_TARGET_REACHED",
                                quantity, round(option.bid, 3), False, diagnostics)
    if option_target:
        if option.spread_pct > config.max_entry_spread_pct:
            return IntradayDecision(position.option_symbol, "WAIT", "TARGET_EXIT_WIDE_SPREAD",
                                    diagnostics=diagnostics)
        return IntradayDecision(position.option_symbol, "PLACE_EXIT", "OPTION_TARGET_REACHED",
                                position.contracts, round(option.bid, 3), False,
                                diagnostics)
    if structure_target and position.partial_exit_taken:
        return IntradayDecision(position.option_symbol, "HOLD",
                                "PARTIAL_TARGET_ALREADY_TAKEN",
                                diagnostics=diagnostics)
    return IntradayDecision(position.option_symbol, "HOLD", "NO_INTRADAY_EXIT", diagnostics=diagnostics)
