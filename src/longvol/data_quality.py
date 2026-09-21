from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .models import Bar, Config, FundamentalSnapshot, OptionQuote, ShortSnapshot


@dataclass(frozen=True)
class DataQuality:
    ok: bool
    checks: dict[str, bool]
    issues: tuple[str, ...]


def _business_day_distance(start: date, end: date) -> int:
    if end <= start:
        return 0
    return sum(1 for i in range(1, (end - start).days + 1)
               if (start.fromordinal(start.toordinal() + i)).weekday() < 5)


def validate_bars(bars: list[Bar], as_of: date, config: Config) -> DataQuality:
    ordered = all(a.day <= b.day for a, b in zip(bars, bars[1:]))
    unique = len({b.day for b in bars}) == len(bars)
    numeric = all(
        b.volume >= 0 and b.low > 0 and b.low <= min(b.open, b.close)
        and b.high >= max(b.open, b.close) and b.high >= b.low
        for b in bars
    )
    enough = len(bars) >= config.min_history_bars
    not_future = bool(bars and bars[-1].day <= as_of)
    fresh = bool(bars and _business_day_distance(bars[-1].day, as_of) <= config.max_bar_age_days)
    exact_as_of = bool(bars and bars[-1].day == as_of) if config.require_bar_on_as_of else True
    checks = {"bars_ordered": ordered, "bars_unique": unique, "bars_valid": numeric,
              "bars_enough": enough, "bars_not_future": not_future, "bars_fresh": fresh,
              "bars_exact_as_of": exact_as_of}
    labels = {
        "bars_ordered": "OHLCV is not sorted", "bars_unique": "duplicate daily bars",
        "bars_valid": "invalid OHLCV values", "bars_enough": "insufficient daily history",
        "bars_not_future": "OHLCV contains future data", "bars_fresh": "stale daily OHLCV",
        "bars_exact_as_of": "latest daily bar is not the as-of trading date",
    }
    return DataQuality(all(checks.values()), checks, tuple(labels[k] for k, v in checks.items() if not v))


def validate_profile_bars(bars: list[Bar] | None, as_of: date, config: Config) -> DataQuality:
    available = bool(bars)
    enough = bool(bars and len(bars) >= config.profile_min_bars)
    numeric = bool(bars and all(b.volume >= 0 and b.low > 0 and b.high >= b.low for b in bars))
    not_future = bool(bars and max(b.day for b in bars) <= as_of)
    fresh = bool(bars and _business_day_distance(max(b.day for b in bars), as_of) <= config.max_bar_age_days)
    checks = {"profile_available": available, "profile_enough": enough, "profile_valid": numeric,
              "profile_not_future": not_future, "profile_fresh": fresh}
    issues = tuple({"profile_available": "missing intraday profile bars",
                    "profile_enough": "insufficient intraday profile bars",
                    "profile_valid": "invalid intraday profile bars",
                    "profile_not_future": "intraday profile contains future data",
                    "profile_fresh": "intraday profile is stale"}[k]
                   for k, v in checks.items() if not v)
    return DataQuality(all(checks.values()), checks, issues)


def validate_options(options: Iterable[OptionQuote], as_of: date, config: Config) -> DataQuality:
    items = list(options)
    available = bool(items)
    valid_items = [o for o in items if (
        o.expiry > as_of and o.strike > 0 and o.bid >= 0 and o.ask >= o.bid
        and o.iv > 0 and -1 <= o.delta <= 1 and o.gamma >= 0
    )]
    valid = bool(valid_items)
    timestamp_ok = (bool(valid_items) and all(
        o.quote_time is not None and o.quote_time.date() == as_of for o in valid_items)
                    if config.require_option_quote_timestamp else True)
    checks = {"options_available": available, "options_valid": valid,
              "option_quotes_as_of": timestamp_ok}
    labels = {"options_available": "missing option chain", "options_valid": "invalid option quotes",
              "option_quotes_as_of": "option quote timestamps are missing or stale"}
    issues = tuple(labels[k] for k, v in checks.items() if not v)
    return DataQuality(all(checks.values()), checks, issues)


def validate_context(short: ShortSnapshot | None, fundamentals: FundamentalSnapshot | None,
                     as_of: date, config: Config) -> DataQuality:
    short_date = None if short is None else (short.short_volume_date or short.day)
    short_flow_fresh = bool(short_date and 0 <= (as_of - short_date).days <= config.short_volume_max_age_days)
    interest_date = short.short_interest_date if short else None
    short_interest_fresh = bool(interest_date and 0 <= (as_of - interest_date).days <= config.short_interest_max_age_days)
    short_fresh = short_flow_fresh or short_interest_fresh
    fundamental_date = fundamentals.as_of if fundamentals else None
    fundamental_fresh = bool(fundamentals and fundamental_date and
                             0 <= (as_of - fundamental_date).days <= config.max_fundamental_age_days)
    fundamental_reviewed = bool(fundamentals and not fundamentals.needs_review and
                                fundamentals.confidence is not None and
                                fundamentals.confidence >= config.min_fundamental_confidence)
    checks = {"short_data_fresh": short_fresh, "short_flow_fresh": short_flow_fresh,
              "short_interest_fresh": short_interest_fresh, "fundamental_fresh": fundamental_fresh,
              "fundamental_reviewed": fundamental_reviewed}
    labels = {"short_data_fresh": "short-flow data missing or stale",
              "short_flow_fresh": "daily short-volume flow missing or stale",
              "short_interest_fresh": "short-interest snapshot missing or stale",
              "fundamental_fresh": "fundamental review missing or stale",
              "fundamental_reviewed": "fundamental review is low-confidence or needs review"}
    required = short_fresh and fundamental_fresh and fundamental_reviewed
    return DataQuality(required, checks, tuple(labels[k] for k, v in checks.items() if not v))
