from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import mean, pstdev, variance
from typing import Iterable, Mapping

from .models import Bar, OptionQuote, ShortSnapshot


def _mean(values: list[float]) -> float:
    return mean(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    return pstdev(values) if len(values) > 1 else 0.0


def true_ranges(bars: list[Bar]) -> list[float]:
    out: list[float] = []
    previous = None
    for b in bars:
        out.append(max(b.high - b.low, abs(b.high - previous), abs(b.low - previous)) if previous else b.high - b.low)
        previous = b.close
    return out


def atr(bars: list[Bar], window: int = 14) -> float:
    return _mean(true_ranges(bars)[-window:])


def realized_vol(bars: list[Bar], window: int = 20) -> float:
    closes = [b.close for b in bars[-(window + 1):]]
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return _stdev(returns) * math.sqrt(252) if returns else 0.0


def yang_zhang_vol(bars: list[Bar], window: int = 20) -> float:
    """Annualized Yang–Zhang OHLC volatility estimator.

    It separates overnight and intraday variance and uses the
    Rogers–Satchell term, making it more informative than close-to-close RV
    when panic gaps and large intraday ranges are present.
    """
    sample = bars[-(window + 1):]
    if len(sample) < 3:
        return 0.0
    overnight, open_close, rs = [], [], []
    for previous, b in zip(sample, sample[1:]):
        if min(previous.close, b.open, b.high, b.low, b.close) <= 0:
            continue
        overnight.append(math.log(b.open / previous.close))
        open_close.append(math.log(b.close / b.open))
        rs.append(math.log(b.high / b.open) * math.log(b.high / b.close) +
                  math.log(b.low / b.open) * math.log(b.low / b.close))
    if len(overnight) < 2:
        return 0.0
    n = len(overnight)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    variance_yz = variance(overnight) + k * variance(open_close) + (1 - k) * mean(rs)
    return math.sqrt(max(0.0, variance_yz) * 252)


def downside_features(bars: list[Bar], window: int = 20) -> dict[str, float]:
    closes = [b.close for b in bars[-(window + 1):]]
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    negative = [r for r in returns if r < 0]
    return {
        "downside_return_share": len(negative) / len(returns) if returns else 0.0,
        "downside_vol": _stdev(negative) * math.sqrt(252) if negative else 0.0,
    }


def residual_return_metrics(
    bars: list[Bar], candidate_values: Mapping[str, float] | None = None,
    window: int = 5,
) -> dict[str, float | bool | str | None]:
    """Return point-in-time raw and benchmark-residual stock returns.

    The current screener supplies ``screen_change_5d`` but does not yet
    supply market/sector benchmark returns.  Canonical optional inputs are
    ``screen_market_change_5d``, ``screen_sector_change_5d``,
    ``screen_market_beta`` and ``screen_sector_beta``.  Missing benchmark
    inputs remain ``None`` rather than being silently replaced by zero.
    """
    values = candidate_values or {}

    def finite_value(name: str) -> float | None:
        try:
            value = float(values[name])
        except (KeyError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    stock_return = finite_value("screen_change_5d")
    stock_source = "candidate_screener"
    if stock_return is None:
        stock_source = "local_daily_bars"
        stock_return = (
            bars[-1].close / bars[-(window + 1)].close - 1
            if len(bars) > window and bars[-(window + 1)].close > 0 else None
        )
    market_return = finite_value("screen_market_change_5d")
    sector_return = finite_value("screen_sector_change_5d")
    market_beta = finite_value("screen_market_beta")
    sector_beta = finite_value("screen_sector_beta")
    market_beta = market_beta if market_beta is not None else 1.0
    sector_beta = sector_beta if sector_beta is not None else 1.0
    market_residual = (
        stock_return - market_beta * market_return
        if stock_return is not None and market_return is not None else None
    )
    sector_residual = (
        stock_return - sector_beta * sector_return
        if stock_return is not None and sector_return is not None else None
    )
    return {
        "stock_return_5d": stock_return,
        "stock_return_5d_source": stock_source,
        "market_return_5d": market_return,
        "sector_return_5d": sector_return,
        "market_beta_input": market_beta,
        "sector_beta_input": sector_beta,
        "market_residual_return_5d": market_residual,
        "sector_residual_return_5d": sector_residual,
        "market_residual_available": market_residual is not None,
        "sector_residual_available": sector_residual is not None,
    }


def volume_zscore(bars: list[Bar], window: int = 20) -> float:
    if len(bars) < window + 1:
        return 0.0
    history = [b.volume for b in bars[-window - 1:-1]]
    sd = _stdev(history)
    return (bars[-1].volume - _mean(history)) / sd if sd else 0.0


def detect_panic_start(bars: list[Bar], lookback: int = 60) -> int:
    """Return the peak that began the largest peak-to-later-trough drawdown."""
    start = max(0, len(bars) - lookback)
    segment = bars[start:]
    if not segment:
        return 0
    running_peak = segment[0].close
    running_peak_idx = 0
    worst_drawdown = 0.0
    best_peak_idx = 0
    for i, bar in enumerate(segment):
        if bar.close > running_peak:
            running_peak = bar.close
            running_peak_idx = i
        drawdown = bar.close / running_peak - 1 if running_peak else 0.0
        if drawdown < worst_drawdown:
            worst_drawdown = drawdown
            best_peak_idx = running_peak_idx
    return start + best_peak_idx


def panic_metrics(bars: list[Bar], min_volume_zscore: float = 1.5,
                  min_range_atr: float = 1.5, max_close_location: float = .30
                  ) -> dict[str, float | bool | int | str | None]:
    if len(bars) < 20:
        return {"drawdown_60d": 0.0, "drawdown_5d": 0.0, "panic_start": 0, "panic_active": False}
    close = bars[-1].close
    high60 = max(b.close for b in bars[-60:])
    high5 = max(b.close for b in bars[-6:-1])
    start = detect_panic_start(bars)
    previous_close = bars[-2].close if len(bars) > 1 else close
    day_range = bars[-1].high - bars[-1].low
    prior_atr = atr(bars[:-1], 14) if len(bars) > 15 else day_range
    close_location = (close - bars[-1].low) / day_range if day_range else 0.5
    one_day_return = close / previous_close - 1 if previous_close else 0.0
    gap_return = bars[-1].open / previous_close - 1 if previous_close else 0.0
    yz = yang_zhang_vol(bars, 20)
    yz_prior = yang_zhang_vol(bars[:-10], 20) if len(bars) >= 40 else 0.0
    down = downside_features(bars, 20)
    recent_events = []
    for i in range(max(1, len(bars) - 5), len(bars)):
        b = bars[i]
        prev = bars[i - 1].close
        span = b.high - b.low
        prior_atr_i = atr(bars[:i], 14) if i > 15 else span
        history = [x.volume for x in bars[max(0, i - 20):i]]
        sd = _stdev(history)
        vz = (b.volume - _mean(history)) / sd if sd else 0.0
        recent_events.append({
            "index": i,
            "day": b.day.isoformat(),
            "return": b.close / prev - 1 if prev else 0.0,
            "gap": b.open / prev - 1 if prev else 0.0,
            "range_atr": span / prior_atr_i if prior_atr_i else 0.0,
            "close_location": (b.close - b.low) / span if span else 0.5,
            "volume_zscore": vz,
        })
    # The event used to anchor the post-panic sequence must contain the
    # abnormal volume and price/range shock on the same bar.  Combining the
    # maximum volume z-score from one day with the worst return from another
    # would manufacture an event that never occurred.
    shock_events = [x for x in recent_events if (
        x["volume_zscore"] >= min_volume_zscore and
        (x["return"] <= -.05 or x["gap"] <= -.02 or
         x["range_atr"] >= min_range_atr)
    )]
    last_shock = shock_events[-1] if shock_events else None
    # Every exported ``recent_shock_*`` field describes the same anchored bar.
    # Keep the legacy field names for downstream compatibility, but never
    # manufacture a composite event from independent five-day extrema.
    shock_return = float(last_shock["return"]) if last_shock else 0.0
    shock_gap = float(last_shock["gap"]) if last_shock else 0.0
    shock_range = float(last_shock["range_atr"]) if last_shock else 0.0
    shock_volume = float(last_shock["volume_zscore"]) if last_shock else 0.0
    forced_event = any(x["volume_zscore"] >= min_volume_zscore and x["range_atr"] >= min_range_atr and
                       x["close_location"] <= max_close_location and
                       (x["return"] <= -.05 or x["gap"] <= -.02) for x in recent_events)
    return {
        "drawdown_60d": close / high60 - 1 if high60 else 0.0,
        "drawdown_5d": close / high5 - 1 if high5 else 0.0,
        "panic_start": start,
        "panic_active": bool(start < len(bars) - 2 and bars[start].close > 0 and
                             close / bars[start].close - 1 <= -0.10),
        "one_day_return": one_day_return,
        "gap_return": gap_return,
        "range_atr": day_range / prior_atr if prior_atr else 0.0,
        "close_location": close_location,
        "volume_zscore": volume_zscore(bars),
        "yang_zhang_vol": yz,
        "yang_zhang_ratio": yz / yz_prior if yz_prior else 1.0,
        **down,
        "recent_shock_return": shock_return,
        "recent_shock_gap": shock_gap,
        "recent_shock_range_atr": shock_range,
        "recent_shock_volume_zscore": shock_volume,
        "recent_shock_event": last_shock is not None,
        "recent_shock_index": int(last_shock["index"]) if last_shock else None,
        "recent_shock_day": last_shock["day"] if last_shock else None,
        "sessions_since_recent_shock": (
            len(bars) - 1 - int(last_shock["index"]) if last_shock else None
        ),
        "recent_forced_liquidation_event": forced_event,
    }


def compression_metrics(bars: list[Bar], atr_threshold: float = 0.85,
                        rv_threshold: float = 0.85,
                        volume_threshold: float = 0.85,
                        shock_index: int | None = None
                        ) -> dict[str, float | bool | None]:
    if len(bars) < 40:
        return {
            "atr_ratio": 1.0, "rv_ratio": 1.0, "volume_ratio": 1.0,
            "atr_compressed": False, "rv_compressed": False,
            "volume_compressed": False, "volatility_compression_count": 0,
            "post_shock_volume_ratio": None,
            "selling_pressure_compressed": False,
            "compression_count": 0, "raw_compressed": False,
            "compressed": False,
        }
    recent_atr = atr(bars, 10)
    prior_atr = _mean(true_ranges(bars[-30:-10]))
    recent_rv = realized_vol(bars, 10)
    prior_rv = realized_vol(bars[:-10], 20)
    recent_vol = _mean([b.volume for b in bars[-5:]])
    prior_vol = _mean([b.volume for b in bars[-25:-5]])
    ratios = {
        "atr_ratio": recent_atr / prior_atr if prior_atr else 1.0,
        "rv_ratio": recent_rv / prior_rv if prior_rv else 1.0,
        "volume_ratio": recent_vol / prior_vol if prior_vol else 1.0,
    }
    atr_compressed = ratios["atr_ratio"] <= atr_threshold
    rv_compressed = ratios["rv_ratio"] <= rv_threshold
    volume_compressed = ratios["volume_ratio"] <= volume_threshold
    post_shock_volume_ratio = None
    if shock_index is not None and 0 <= shock_index < len(bars) - 1:
        post_shock = bars[shock_index + 1:]
        post_shock_volume = _mean([b.volume for b in post_shock[-5:]])
        shock_volume = bars[shock_index].volume
        post_shock_volume_ratio = (
            post_shock_volume / shock_volume if shock_volume > 0 else None
        )
    selling_pressure_compressed = bool(
        post_shock_volume_ratio is not None and
        post_shock_volume_ratio <= volume_threshold
    )
    volatility_count = int(atr_compressed) + int(rv_compressed)
    count = volatility_count + int(volume_compressed)
    # A high-volume negative shock may itself carry the liquidity-premium
    # signal.  Exhaustion is therefore measured as volume decay *after that
    # shock*, not as volume falling below the pre-event normal regime.  ATR
    # and RV are related price-volatility measures, so at least one—not both
    # as a two-of-three shortcut—must contract as well.
    raw_compressed = bool(selling_pressure_compressed and volatility_count >= 1)
    return {
        **ratios,
        "atr_compressed": atr_compressed,
        "rv_compressed": rv_compressed,
        "volume_compressed": volume_compressed,
        "post_shock_volume_ratio": post_shock_volume_ratio,
        "selling_pressure_compressed": selling_pressure_compressed,
        "volatility_compression_count": volatility_count,
        "compression_count": count,
        "raw_compressed": raw_compressed,
        "compressed": raw_compressed,
    }


@dataclass(frozen=True)
class VolumeProfile:
    bins: dict[float, float]
    peak_price: float
    support_price: float | None
    resistance_price: float | None
    overhead_share_10pct: float
    support_share_10pct: float
    overhead_density_ratio: float
    value_area_low: float | None
    value_area_high: float | None
    high_volume_nodes: tuple[float, ...]
    source_quality: str


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = max(0, min(len(values) - 1, round((len(values) - 1) * q)))
    return values[idx]


def volume_profile(bars: list[Bar], bin_size: float | None = None,
                   value_area_pct: float = 0.70, source_quality: str = "daily_fallback") -> VolumeProfile:
    """Build an anchored profile by distributing each lower-timeframe bar.

    With intraday bars, volume is spread uniformly across every intersected
    price row. It cannot recreate trade-at-price data, but it is materially
    closer to a TradingView-style lower-timeframe profile than assigning a
    whole daily bar to one typical price. Daily input remains a diagnostic
    fallback and is labelled as such.
    """
    if not bars:
        return VolumeProfile({}, 0.0, None, None, 1.0, 0.0, float("inf"), None, None, (), "missing")
    price = bars[-1].close
    span = max(b.high for b in bars) - min(b.low for b in bars)
    step = bin_size or max(price * 0.005, span / 40 if span else price * 0.005)
    bins: dict[float, float] = defaultdict(float)
    for b in bars:
        low_key = math.floor(b.low / step)
        high_key = math.floor(b.high / step)
        if high_key < low_key:
            continue
        rows = list(range(low_key, high_key + 1))
        allocation = b.volume / len(rows) if rows else 0.0
        for row in rows:
            bins[round(row * step, 8)] += allocation
    peak = max(bins, key=bins.get)
    total = sum(bins.values()) or 1.0
    overhead = sum(v for p, v in bins.items() if price < p <= price * 1.10) / total
    support = sum(v for p, v in bins.items() if price * 0.90 <= p < price) / total
    ordered = sorted(bins)
    threshold = _percentile(list(bins.values()), .75)
    hvn = []
    for i, level in enumerate(ordered):
        left = bins[ordered[i - 1]] if i else -1.0
        right = bins[ordered[i + 1]] if i + 1 < len(ordered) else -1.0
        if bins[level] >= threshold and bins[level] >= left and bins[level] >= right:
            hvn.append(level)
    below = [p for p in hvn if p < price]
    above = [p for p in hvn if p > price]
    support_price = max(below, default=None)
    resistance_price = min(above, default=None)
    overhead_rows = [v for p, v in bins.items() if price < p <= price * 1.10]
    avg_all = total / len(bins) if bins else 0.0
    overhead_density = (_mean(overhead_rows) / avg_all) if overhead_rows and avg_all else 0.0

    # TradingView-style contiguous value area expansion around the POC.
    target = total * max(0.0, min(value_area_pct, 1.0))
    peak_i = ordered.index(peak)
    included = {peak_i}
    accumulated = bins[peak]
    lo_i = hi_i = peak_i
    while accumulated < target and (lo_i > 0 or hi_i + 1 < len(ordered)):
        left_i = lo_i - 1 if lo_i > 0 else None
        right_i = hi_i + 1 if hi_i + 1 < len(ordered) else None
        left_v = bins[ordered[left_i]] if left_i is not None else -1.0
        right_v = bins[ordered[right_i]] if right_i is not None else -1.0
        if right_v >= left_v and right_i is not None:
            hi_i = right_i; included.add(hi_i); accumulated += right_v
        elif left_i is not None:
            lo_i = left_i; included.add(lo_i); accumulated += left_v
        else:
            break
    val = ordered[min(included)] if included else None
    vah = ordered[max(included)] if included else None
    return VolumeProfile(dict(bins), peak, support_price, resistance_price, overhead, support,
                         overhead_density, val, vah, tuple(hvn), source_quality)


def gamma_walls(options: Iterable[OptionQuote], spot: float) -> list[dict[str, float | str]]:
    """Estimate strike-level GEX concentration from an option chain.

    Dealer side is not observable from an ordinary chain. The sign below is a
    transparent call-minus-put proxy, not a claim about actual dealer inventory.
    """
    by_strike: dict[float, dict[str, float]] = defaultdict(lambda: {"call": 0.0, "put": 0.0, "oi": 0.0})
    for o in options:
        gex = abs(o.gamma) * max(o.open_interest, 0) * o.multiplier * spot * spot * 0.01
        side = "call" if o.right == "C" else "put"
        by_strike[o.strike][side] += gex
        by_strike[o.strike]["oi"] += o.open_interest
    total_abs_gex = sum(x["call"] + x["put"] for x in by_strike.values())
    walls = []
    for strike, x in by_strike.items():
        absolute = x["call"] + x["put"]
        walls.append({"strike": strike, "proxy_gex": x["call"] - x["put"], "abs_gex": absolute,
                      "abs_gex_share": absolute / total_abs_gex if total_abs_gex else 0.0, "oi": x["oi"],
                      "side": "call" if x["call"] >= x["put"] else "put", "dealer_side_known": "false", "quality": "oi_gamma_proxy"})
    return sorted(walls, key=lambda x: float(x["abs_gex"]), reverse=True)


def short_pressure(snapshot: ShortSnapshot | None) -> dict[str, float | bool | None]:
    if snapshot is None:
        return {"available": False, "si_change": None, "svr_change": None, "short_interest_weaker": False,
                "short_flow_weaker": False, "worsening": False, "weakening": False}
    si_change = None
    if snapshot.short_interest is not None and snapshot.short_interest_5d_ago:
        si_change = snapshot.short_interest / snapshot.short_interest_5d_ago - 1
    svr_change = None
    baseline = snapshot.short_volume_ratio_20d_avg
    if baseline is None:
        baseline = snapshot.short_volume_ratio_5d_ago
    if snapshot.short_volume_ratio is not None and baseline is not None:
        svr_change = snapshot.short_volume_ratio - baseline
    si_weaker = bool(si_change is not None and si_change <= -0.02)
    flow_weaker = bool(svr_change is not None and svr_change <= -0.03)
    worsening = bool((si_change is not None and si_change > 0.05) or
                     (svr_change is not None and svr_change > 0.05))
    weakening = bool((si_weaker or flow_weaker) and not worsening)
    return {"available": si_change is not None or svr_change is not None, "si_change": si_change,
            "svr_change": svr_change, "short_interest_weaker": si_weaker,
            "short_flow_weaker": flow_weaker, "worsening": worsening, "weakening": weakening}


def option_quality(options: Iterable[OptionQuote], as_of, config) -> tuple[OptionQuote | None, dict[str, float | bool | None]]:
    candidates = []
    for o in options:
        dte = (o.expiry - as_of).days
        if dte < config.min_dte or dte > config.max_dte or o.mid <= 0 or o.ask <= 0 or o.ask < o.bid or o.right != config.preferred_right:
            continue
        if o.open_interest < config.min_option_open_interest or o.volume < config.min_option_volume:
            continue
        delta = abs(o.delta)
        if not config.target_delta_low <= delta <= config.target_delta_high:
            continue
        candidates.append((o, dte))
    if not candidates:
        return None, {"available": False, "spread_pct": None, "dte": None, "delta": None, "iv": None, "entry_price": None}
    candidates.sort(key=lambda x: (x[0].spread_pct, abs(abs(x[0].delta) - 0.40), abs(x[1] - 90)))
    best, dte = candidates[0]
    return best, {"available": True, "spread_pct": best.spread_pct, "dte": dte, "delta": abs(best.delta),
                  "iv": best.iv, "iv_percentile": best.iv_percentile, "entry_price": best.ask,
                  "option_open_interest": best.open_interest, "option_volume": best.volume}


def historical_percentile(current: float | None, history: Iterable[float]) -> float | None:
    values = [float(x) for x in history if x is not None and float(x) > 0]
    if current is None or not values:
        return None
    return sum(x <= current for x in values) / len(values)


def option_surface_metrics(options: Iterable[OptionQuote], spot: float, as_of) -> dict[str, float | bool | None]:
    """Summarize the option surface without pretending to know dealer inventory.

    Skew is the IV difference between puts and calls around 25-delta. Term
    structure is the change in near-ATM IV from roughly 45 DTE to 120 DTE.
    Both are diagnostics; they become gates in strategy.evaluate.
    """
    items = [o for o in options if o.mid > 0 and o.iv > 0 and (o.expiry - as_of).days > 0]
    expiries = sorted({o.expiry for o in items})
    near_expiry = min((x for x in expiries if 20 <= (x - as_of).days <= 75),
                      key=lambda x: abs((x - as_of).days - 45), default=None)
    far_expiry = min((x for x in expiries if 90 <= (x - as_of).days <= 180),
                     key=lambda x: abs((x - as_of).days - 120), default=None)
    def closest(right: str, delta: float, expiry):
        pool = [o for o in items if o.right == right and o.expiry == expiry]
        return min(pool, key=lambda o: abs(abs(o.delta) - delta), default=None)
    put25 = closest("P", .25, near_expiry)
    call25 = closest("C", .25, near_expiry)
    near = [o for o in items if o.expiry == near_expiry and abs(abs(o.delta) - .5) <= .15]
    far = [o for o in items if o.expiry == far_expiry and abs(abs(o.delta) - .5) <= .15]
    near_iv = sum(o.iv for o in near) / len(near) if near else None
    far_iv = sum(o.iv for o in far) / len(far) if far else None
    skew = put25.iv - call25.iv if put25 and call25 else None
    slope = far_iv - near_iv if near_iv is not None and far_iv is not None else None
    iv_values = [o.iv_percentile for o in items if o.iv_percentile is not None]
    total_volume = sum(max(o.volume, 0) for o in items)
    put_volume = sum(max(o.volume, 0) for o in items if o.right == "P")
    call_volume = sum(max(o.volume, 0) for o in items if o.right == "C")
    total_oi = sum(max(o.open_interest, 0) for o in items)
    put_oi = sum(max(o.open_interest, 0) for o in items if o.right == "P")
    call_oi = sum(max(o.open_interest, 0) for o in items if o.right == "C")
    put_25d_delta_distance = abs(abs(put25.delta) - .25) if put25 else None
    call_25d_delta_distance = abs(abs(call25.delta) - .25) if call25 else None
    put_25d_coverage = bool(
        put_25d_delta_distance is not None and put_25d_delta_distance <= .10
    )
    call_25d_coverage = bool(
        call_25d_delta_distance is not None and call_25d_delta_distance <= .10
    )
    near_atm_coverage = bool(near)
    far_atm_coverage = bool(far)
    put_flow_coverage = bool(put_volume > 0 and call_volume > 0)
    return {"surface_available": bool(items), "put_25d_iv": put25.iv if put25 else None,
            "call_25d_iv": call25.iv if call25 else None, "put_call_25d_skew": skew,
            "near_atm_iv": near_iv, "far_atm_iv": far_iv, "term_structure_slope": slope,
            "iv_percentile": sum(iv_values) / len(iv_values) if iv_values else None,
            "put_volume_share": put_volume / total_volume if total_volume else None,
            "put_call_volume_ratio": put_volume / call_volume if call_volume else None,
            "put_call_oi_ratio": put_oi / call_oi if call_oi else None,
            "total_option_volume": total_volume, "total_option_oi": total_oi,
            "near_expiry": near_expiry.isoformat() if near_expiry else None,
            "far_expiry": far_expiry.isoformat() if far_expiry else None,
            "put_25d_delta_distance": put_25d_delta_distance,
            "call_25d_delta_distance": call_25d_delta_distance,
            "put_25d_coverage": put_25d_coverage,
            "call_25d_coverage": call_25d_coverage,
            "skew_coverage": put_25d_coverage and call_25d_coverage,
            "near_atm_contract_count": len(near),
            "far_atm_contract_count": len(far),
            "near_atm_coverage": near_atm_coverage,
            "far_atm_coverage": far_atm_coverage,
            "term_structure_coverage": near_atm_coverage and far_atm_coverage,
            "positive_put_volume": put_volume > 0,
            "positive_call_volume": call_volume > 0,
            "put_flow_coverage": put_flow_coverage}


def option_panic_metrics(options: Iterable[OptionQuote], spot: float, as_of, previous_near_atm_iv: float | None = None, min_signals: int = 2) -> dict[str, float | bool | int | None]:
    surface = option_surface_metrics(options, spot, as_of)
    skew = surface["put_call_25d_skew"]
    slope = surface["term_structure_slope"]
    pcr = surface["put_call_volume_ratio"]
    put_share = surface["put_volume_share"]
    iv_jump = None
    if previous_near_atm_iv and surface["near_atm_iv"] is not None:
        iv_jump = float(surface["near_atm_iv"]) / previous_near_atm_iv - 1
    availability = {
        "iv_jump_available": bool(
            previous_near_atm_iv and previous_near_atm_iv > 0 and
            surface["near_atm_coverage"]
        ),
        "skew_stress_available": bool(surface["skew_coverage"]),
        "term_inverted_available": bool(surface["term_structure_coverage"]),
        "put_protection_flow_available": bool(surface["put_flow_coverage"]),
    }
    signals = {
        "iv_jump": bool(availability["iv_jump_available"] and
                        iv_jump is not None and iv_jump >= 0.10),
        "skew_stress": bool(availability["skew_stress_available"] and
                            skew is not None and float(skew) >= 0.08),
        "term_inverted": bool(availability["term_inverted_available"] and
                              slope is not None and float(slope) <= -0.05),
        # Volume cannot reveal buy-vs-sell direction; this is a protection-flow proxy.
        "put_protection_flow": bool(
            availability["put_protection_flow_available"] and
            pcr is not None and put_share is not None and
            float(pcr) >= 1.5 and float(put_share) >= 0.55
        ),
    }
    available_family_count = sum(availability.values())
    coverage_sufficient = available_family_count >= min_signals
    return {
        **surface,
        "iv_jump_pct": iv_jump,
        "option_stress_signal_count": sum(signals.values()),
        "option_stress_available_family_count": available_family_count,
        "option_stress_required_family_count": min_signals,
        "option_surface_coverage_sufficient": coverage_sufficient,
        **availability,
        **signals,
        "option_panic_gate": bool(
            coverage_sufficient and sum(signals.values()) >= min_signals
        ),
    }


def aligned_option_panic_metrics(
    options: Iterable[OptionQuote], spot: float, shock_day: date | None,
    snapshot_day: date | None, previous_near_atm_iv: float | None = None,
    min_signals: int = 2, require_quote_timestamp: bool = True,
) -> dict[str, float | bool | int | str | None]:
    """Evaluate option stress only from a snapshot aligned to the shock day.

    A caller-supplied snapshot date is not enough when quote timestamps prove a
    different date.  Missing historical data is represented as ``UNKNOWN`` and
    always fails the option-panic gate; it must never be backfilled with the
    entry-day chain.
    """
    items = list(options)
    empty = option_panic_metrics(
        [], spot, shock_day or snapshot_day or date.min,
        previous_near_atm_iv, min_signals,
    )

    quote_days = sorted({
        option.quote_time.date() for option in items
        if option.quote_time is not None
    })
    missing_quote_times = sum(option.quote_time is None for option in items)

    def unknown(alignment_status: str, alignment_ok: bool = False):
        return {
            **empty,
            "option_panic_gate": False,
            "option_panic_known": False,
            "option_panic_status": "UNKNOWN",
            "option_panic_alignment_ok": alignment_ok,
            "option_panic_alignment_status": alignment_status,
            "option_panic_shock_day": shock_day.isoformat() if shock_day else None,
            "option_panic_snapshot_day": snapshot_day.isoformat() if snapshot_day else None,
            "option_panic_quote_days": " | ".join(day.isoformat() for day in quote_days),
            "option_panic_snapshot_contracts": len(items),
            "option_panic_missing_quote_times": missing_quote_times,
        }

    if shock_day is None:
        return unknown("NO_SHOCK_EVENT")
    if snapshot_day is None:
        return unknown("MISSING_SHOCK_SNAPSHOT")
    if snapshot_day != shock_day:
        return unknown("SNAPSHOT_DAY_MISMATCH")
    if any(day != shock_day for day in quote_days):
        return unknown("QUOTE_DAY_MISMATCH")
    if require_quote_timestamp and missing_quote_times:
        return unknown("QUOTE_TIME_MISSING")
    if not items:
        return unknown("ALIGNED_EMPTY_SNAPSHOT", alignment_ok=True)

    observed = option_panic_metrics(
        items, spot, shock_day, previous_near_atm_iv, min_signals,
    )
    if not observed["option_surface_coverage_sufficient"]:
        return {
            **observed,
            "option_panic_gate": False,
            "option_panic_known": False,
            "option_panic_status": "UNKNOWN",
            "option_panic_alignment_ok": True,
            "option_panic_alignment_status": "INSUFFICIENT_SURFACE_COVERAGE",
            "option_panic_shock_day": shock_day.isoformat(),
            "option_panic_snapshot_day": snapshot_day.isoformat(),
            "option_panic_quote_days": " | ".join(day.isoformat() for day in quote_days),
            "option_panic_snapshot_contracts": len(items),
            "option_panic_missing_quote_times": missing_quote_times,
        }
    return {
        **observed,
        "option_panic_known": True,
        "option_panic_status": "KNOWN",
        "option_panic_alignment_ok": True,
        "option_panic_alignment_status": "ALIGNED",
        "option_panic_shock_day": shock_day.isoformat(),
        "option_panic_snapshot_day": snapshot_day.isoformat(),
        "option_panic_quote_days": " | ".join(day.isoformat() for day in quote_days),
        "option_panic_snapshot_contracts": len(items),
        "option_panic_missing_quote_times": missing_quote_times,
    }
