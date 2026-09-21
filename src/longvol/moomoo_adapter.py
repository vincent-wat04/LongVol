"""Optional Moomoo OpenD adapter.

The core strategy intentionally depends only on the dataclasses in models.py.
This module is isolated because the `moomoo` SDK and a running OpenD process
are environment-specific. Install the pinned major SDK and validate the
documented response fields against the account's OpenD permissions.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
import math
from numbers import Real
from time import monotonic, sleep
from zoneinfo import ZoneInfo

from .models import Bar, OptionQuote, OptionRegimeSnapshot, ShortSnapshot


MIN_OPEND_SERVER_VERSION = 1010
REQUIRED_OPEND_VERSION = "10.10.7008"


def _now_et() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _pick(row: dict, *names, default=None):
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        if isinstance(value, str) and value.strip().upper() in {
            "", "N/A", "NA", "NAN", "NONE", "NULL",
        }:
            continue
        if isinstance(value, Real) and math.isnan(float(value)):
            continue
        return value
    return default


def normalize_option_quote(row: dict, observed_at: datetime | None = None) -> OptionQuote:
    right = str(_pick(row, "right", "option_type", "put_call", default="")).upper()
    if right in {"1", "1.0", "C", "CALL", "CALL_OPTION"}:
        right = "C"
    elif right in {"2", "2.0", "P", "PUT", "PUT_OPTION"}:
        right = "P"
    else:
        raise ValueError(f"unknown option right: {right!r}")
    expiry = str(_pick(row, "expiry", "expiration_date", "strike_date", "strike_time", "expire_time"))[:10]
    if len(expiry) == 8 and expiry.isdigit():
        expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    iv = float(_pick(row, "iv", "option_implied_volatility", "implied_volatility", default=0) or 0)
    if iv > 3:
        iv /= 100.0
    delta = float(_pick(row, "option_delta", "delta", default=0) or 0)
    if abs(delta) > 1:
        delta /= 100.0
    gamma = float(_pick(row, "option_gamma", "gamma", default=0) or 0)
    raw_time = _pick(row, "quote_time", "update_time", "data_time", default=None)
    quote_time = None
    if raw_time:
        try:
            quote_time = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
        except ValueError:
            quote_time = None
    if quote_time is None:
        quote_time = observed_at
    return OptionQuote(
        symbol=str(_pick(row, "symbol", "code", "option_code")), expiry=date.fromisoformat(expiry),
        strike=float(_pick(row, "strike", "strike_price", "option_strike_price", default=0)), right=right,
        bid=float(_pick(row, "bid", "bid_price", default=0) or 0), ask=float(_pick(row, "ask", "ask_price", default=0) or 0),
        last=float(_pick(row, "last", "last_price", "price", default=0) or 0),
        iv=iv, delta=delta, gamma=gamma,
        open_interest=float(_pick(row, "option_open_interest", "open_interest", "open_interest_qty", default=0) or 0),
        volume=float(_pick(row, "volume", "volume_qty", default=0) or 0),
        quote_time=quote_time,
        multiplier=int(float(_pick(row, "multiplier", "contract_multiplier", "option_contract_multiplier",
                                   "option_contract_size", "lot_size", default=100) or 100)),
        vega=float(_pick(row, "vega", "option_vega", default=0) or 0),
        theta=float(_pick(row, "theta", "option_theta", default=0) or 0),
    )


def normalize_stock_screen_item(item: dict, field_ids: dict[str, int]) -> dict:
    """Flatten a Stock Screening V2 item using SDK enum ids."""
    by_id = {}
    for result in item.get("results", []):
        try:
            name = int(result.get("property", {}).get("name"))
        except (TypeError, ValueError):
            continue
        value = next((result.get(k) for k in ("sval", "dval", "ival", "aval")
                      if result.get(k) is not None), None)
        by_id[name] = value
    return {name: by_id.get(field_id) for name, field_id in field_ids.items()}


def normalize_history_quota(data) -> dict[str, object]:
    """Normalize historical K-line quota tuples across OpenD versions."""
    if not isinstance(data, (tuple, list)) or len(data) < 2:
        raise ValueError(f"unexpected historical K-line quota response: {data!r}")
    try:
        used, remaining = int(data[0]), int(data[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid historical K-line quota counts: {data!r}") from exc
    details: list[dict] = []
    for value in data[2:]:
        if isinstance(value, dict):
            details.append(value)
        elif isinstance(value, (tuple, list)):
            details.extend(item for item in value if isinstance(item, dict))
    codes = sorted({str(item.get("code", "")).upper() for item in details if item.get("code")})
    return {"used": used, "remaining": remaining, "codes": codes}


def normalize_trading_days(data, start: date, end: date) -> list[date]:
    """Normalize OpenD trading-calendar rows into ordered unique dates."""
    if start > end:
        raise ValueError("trading-calendar start must not exceed end")
    if hasattr(data, "to_dict"):
        rows = list(data.to_dict("records"))
    elif isinstance(data, dict):
        rows = [data]
    elif isinstance(data, (list, tuple)):
        rows = [row for row in data if isinstance(row, dict)]
    else:
        raise ValueError(f"unexpected trading-calendar response: {data!r}")
    days: set[date] = set()
    for row in rows:
        raw = _pick(row, "time", "trade_date", "date", default=None)
        if raw is None:
            continue
        try:
            day = (raw.date() if isinstance(raw, datetime) else raw
                   if isinstance(raw, date) else
                   date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
        if start <= day <= end:
            days.add(day)
    return sorted(days)


def _decimal_percent(value) -> float | None:
    if value in (None, "", "N/A"):
        return None
    # This normalizer is only for get_option_underlying_rank. Moomoo documents
    # all IV/HV/rank/percentile/change fields there as values before the % sign.
    return float(value) / 100.0


def normalize_option_regime(row: dict, as_of: date) -> OptionRegimeSnapshot:
    symbol = str(_pick(row, "code", "symbol", default="")).removeprefix("US.").upper()
    if not symbol:
        raise ValueError("option regime row has no symbol")
    raw_day = _pick(
        row, "trading_date", "timestamp_str", "time", "timestamp",
        default=as_of.isoformat(),
    )
    if isinstance(raw_day, datetime):
        day = raw_day.date()
    elif isinstance(raw_day, date):
        day = raw_day
    elif isinstance(raw_day, (int, float)) or str(raw_day).isdigit():
        timestamp = float(raw_day)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        day = datetime.fromtimestamp(
            timestamp, tz=ZoneInfo("America/New_York")).date()
    else:
        day = date.fromisoformat(str(raw_day)[:10])
    snapshot = OptionRegimeSnapshot(
        symbol=symbol, day=day,
        iv=_decimal_percent(_pick(row, "iv", default=None)),
        iv_rank=_decimal_percent(_pick(row, "iv_rank", default=None)),
        iv_percentile=_decimal_percent(_pick(row, "iv_percentile", default=None)),
        iv_change=_decimal_percent(_pick(row, "iv_change", default=None)),
        hv=_decimal_percent(_pick(row, "hv", default=None)),
        hv_change=_decimal_percent(_pick(row, "hv_change", default=None)),
        put_call_volume_ratio=(float(_pick(row, "volume_ratio", "put_call_volume_ratio"))
                               if _pick(row, "volume_ratio", "put_call_volume_ratio")
                               not in (None, "", "N/A") else None),
        put_call_open_interest_ratio=(
            float(_pick(row, "open_interest_ratio", "put_call_open_interest_ratio"))
            if _pick(row, "open_interest_ratio", "put_call_open_interest_ratio")
            not in (None, "", "N/A") else None),
    )
    for name in ("iv_rank", "iv_percentile"):
        value = getattr(snapshot, name)
        if value is not None and not 0 <= value <= 1:
            raise ValueError(f"Moomoo {name} is outside [0, 1]")
    return snapshot


class MoomooProvider:
    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        try:
            from moomoo import OpenQuoteContext  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install the official moomoo SDK and start OpenD before using MoomooProvider") from exc
        self._ctx = OpenQuoteContext(host=host, port=port)
        self._last_server_call: dict[str, float] = {}

    @staticmethod
    def _chunks(values: list[str], size: int = 100):
        for i in range(0, len(values), size):
            yield values[i:i + size]

    def _call(self, method, *args, **kwargs):
        """Retry transient OpenD failures without retrying malformed inputs forever."""
        from moomoo import RET_OK  # type: ignore
        endpoint = getattr(method, "__name__", "unknown")
        # Screening and option-chain endpoints are limited to 10 requests per
        # 30 seconds. Other quote endpoints use a conservative one-second gap.
        interval = 3.05 if endpoint in {
            "get_stock_screen", "get_option_screen", "get_option_underlying_rank",
        } else 1.05
        last = None
        for attempt in range(3):
            wait = interval - (monotonic() - self._last_server_call.get(endpoint, 0.0))
            if wait > 0:
                sleep(wait)
            result = method(*args, **kwargs)
            self._last_server_call[endpoint] = monotonic()
            if result and result[0] == RET_OK:
                return result
            last = result
            if attempt < 2:
                sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"Moomoo OpenD request failed after retries: {last}")

    def close(self) -> None:
        self._ctx.close()

    def get_history_quota(self) -> dict[str, object]:
        """Return current historical K-line quota and recently pulled codes."""
        data = self._call(self._ctx.get_history_kl_quota, get_detail=True)[1]
        return normalize_history_quota(data)

    def get_trading_days(self, start: date, end: date,
                         market: str = "US") -> list[date]:
        """Read the authoritative OpenD market calendar for entry aging."""
        if start > end:
            raise ValueError("trading-calendar start must not exceed end")
        from moomoo import TradeDateMarket  # type: ignore

        market_value = getattr(TradeDateMarket, market.upper(), None)
        if market_value is None:
            raise ValueError(f"unsupported trading-calendar market: {market}")
        data = self._call(
            self._ctx.request_trading_days, market=market_value,
            start=start.isoformat(), end=end.isoformat(),
        )[1]
        return normalize_trading_days(data, start, end)

    def get_bars(self, code: str, start: date, end: date, intraday: bool = False) -> list[Bar]:
        from moomoo import AuType, KLType  # type: ignore
        rows, page_key = [], None
        while True:
            ktype = getattr(KLType, "K_5M") if intraday else KLType.K_DAY
            kwargs = {"start": str(start), "end": str(end), "ktype": ktype, "autype": AuType.QFQ,
                      "max_count": 1000, "extended_time": False}
            if page_key:
                kwargs["page_req_key"] = page_key
            result = self._call(self._ctx.request_history_kline, code, **kwargs)
            frame = result[1]
            rows.extend(frame.to_dict("records"))
            page_key = result[2] if len(result) > 2 else None
            if not page_key:
                break
        output = []
        seen = set()
        for row in rows:
            raw_time = str(_pick(row, "time_key", "time", "date"))
            timestamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00")) if len(raw_time) > 10 else None
            key = timestamp.isoformat() if timestamp else raw_time[:10]
            if key in seen:
                continue
            seen.add(key)
            output.append(Bar(date.fromisoformat(raw_time[:10]), float(row["open"]), float(row["high"]),
                              float(row["low"]), float(row["close"]), float(row["volume"]), timestamp))
        return output

    def get_intraday_bars(self, code: str, start: date, end: date) -> list[Bar]:
        return self.get_bars(code, start, end, intraday=True)

    def _market_snapshots(self, codes: list[str]) -> list[dict]:
        rows: list[dict] = []
        # The endpoint documents a maximum of 400 targets per request.
        for chunk in self._chunks(codes, 400):
            frame = self._call(self._ctx.get_market_snapshot, chunk)[1]
            rows.extend(frame.to_dict("records"))
        return rows

    def get_live_quotes(self, codes: list[str]):
        """Return minimal live quotes for execution checks.

        The broker adapter remains separate from the quote connection. This
        method never submits an order and timestamps every observation in New
        York time so stale quotes fail closed.
        """
        from .broker import LiveQuote

        observed_at = _now_et()
        output = {}
        for row in self._market_snapshots(list(dict.fromkeys(codes))):
            code = str(_pick(row, "code", "option_code", "symbol", default=""))
            if not code:
                continue
            raw_time = _pick(row, "update_time", "data_time", "quote_time", default=None)
            quote_time = None
            if raw_time:
                try:
                    quote_time = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                    if quote_time.tzinfo is None:
                        quote_time = quote_time.replace(tzinfo=ZoneInfo("America/New_York"))
                except ValueError:
                    quote_time = None
            iv = float(_pick(row, "option_implied_volatility", "implied_volatility", "iv", default=0) or 0)
            if iv > 3:
                iv /= 100
            output[code] = LiveQuote(
                code=code, observed_at=observed_at, quote_time=quote_time,
                last=float(_pick(row, "last_price", "price", "last", default=0) or 0),
                bid=float(_pick(row, "bid_price", "bid", default=0) or 0),
                ask=float(_pick(row, "ask_price", "ask", default=0) or 0),
                open=float(_pick(row, "open_price", "open", default=0) or 0) or None,
                high=float(_pick(row, "high_price", "high", default=0) or 0) or None,
                low=float(_pick(row, "low_price", "low", default=0) or 0) or None,
                iv=iv or None,
            )
        return output

    def _option_screen_rows(self, code: str, config, *, flow: bool) -> list[dict]:
        """Fetch a complete bounded option subset using server-side filters."""
        from moomoo import (OptIndicator, OptMarketCategory,  # type: ignore
                            OptUnderlyingIndicator, OptionScreenRequest)

        page_from = 0
        rows: list[dict] = []
        while True:
            request = OptionScreenRequest(market_categories=[OptMarketCategory.US_STOCK])
            request.add_underlying_filter(OptUnderlyingIndicator.STOCK_LIST, values=[code])
            request.add_option_filter(OptIndicator.LEFT_DAY, lower=1, upper=180)
            if flow:
                request.add_option_filter(OptIndicator.VOLUME, lower=config.option_flow_min_volume)
            else:
                request.add_option_filter(OptIndicator.OPEN_INTEREST,
                                          lower=config.option_surface_min_open_interest)
            for field in (
                OptIndicator.STRIKE_PRICE, OptIndicator.STRIKE_DATE_TIMESTAMP,
                OptIndicator.OPTION_TYPE, OptIndicator.PRICE,
                OptIndicator.VOLUME, OptIndicator.OPEN_INTEREST,
                OptIndicator.IMPLIED_VOLATILITY, OptIndicator.DELTA,
                OptIndicator.GAMMA, OptIndicator.VEGA, OptIndicator.THETA,
            ):
                request.add_option_retrieve(field)
            request.add_sort(OptIndicator.OPEN_INTEREST, desc=True)
            request.page_from = page_from
            request.page_count = 200
            last_page, all_count, frame = self._call(self._ctx.get_option_screen, request)[1]
            page = frame.to_dict("records")
            if all_count > config.option_screener_max_contracts:
                raise RuntimeError(
                    f"option screen returned {all_count} contracts for {code}; "
                    "raise option_screener_max_contracts only after reviewing runtime and data volume"
                )
            rows.extend(page)
            if last_page or not page:
                break
            page_from += len(page)
        return rows

    def get_options(self, code: str, as_of: date, config) -> list[OptionQuote]:
        """Build the analysis surface from Option Screening plus live snapshots.

        Two server-side subsets are unioned: OI-bearing contracts for surface
        and gamma concentration, and actively traded contracts for panic flow.
        Tradable call candidates are refreshed through market snapshot so the
        selected contract carries its actual lot size and executable sides.
        """
        observed_at = _now_et()
        if as_of > observed_at.date():
            raise ValueError("option as-of date cannot be in the future")
        historical_catchup = as_of < observed_at.date()
        if historical_catchup:
            if observed_at.time() >= time(9, 0):
                raise ValueError(
                    "prior-session option catch-up must start before 09:00 America/New_York")
            if as_of.weekday() >= 5 or as_of < observed_at.date() - timedelta(days=4):
                raise ValueError(
                    "option catch-up only supports the most recent weekday session candidate")
        by_code: dict[str, dict] = {}
        for flow in (False, True):
            for row in self._option_screen_rows(code, config, flow=flow):
                option_code = str(_pick(row, "code", "option_code", "symbol", default=""))
                if option_code:
                    by_code[option_code] = {**by_code.get(option_code, {}), **row}
        if len(by_code) > config.option_screener_max_contracts:
            raise RuntimeError(
                f"option-screen union returned {len(by_code)} contracts for {code}; "
                "raise option_screener_max_contracts only after reviewing runtime and coverage"
            )
        base_quotes: list[OptionQuote] = []
        for row in by_code.values():
            try:
                base_quotes.append(normalize_option_quote(row, observed_at))
            except (TypeError, ValueError, KeyError):
                continue
        tradable_codes = [
            option.symbol for option in base_quotes
            if option.right == config.preferred_right
            and config.min_dte <= (option.expiry - as_of).days <= config.max_dte
            and config.target_delta_low <= abs(option.delta) <= config.target_delta_high
            and option.open_interest >= config.min_option_open_interest
            and option.volume >= config.min_option_volume
        ]
        # Option Screening has no row-level source timestamp. A historical
        # catch-up therefore refreshes every retained contract through Market
        # Snapshot. A normal same-day run refreshes only tradable contracts to
        # preserve the faster production path.
        refresh_codes = list(by_code) if historical_catchup else tradable_codes
        snapshots = {
            str(_pick(row, "code", "option_code", "symbol")): row
            for row in self._market_snapshots(refresh_codes)
            if _pick(row, "code", "option_code", "symbol")
        }
        output: list[OptionQuote] = []
        observed_dates: Counter[str] = Counter()
        for option_code, row in by_code.items():
            snapshot_row = snapshots.get(option_code)
            if historical_catchup and not snapshot_row:
                continue
            merged = {**row, **(snapshot_row or {})}
            for static_field in ("option_type", "strike_date", "strike_price"):
                if row.get(static_field) not in (None, ""):
                    merged[static_field] = row[static_field]
            try:
                option = normalize_option_quote(
                    merged, None if historical_catchup else observed_at)
            except (TypeError, ValueError, KeyError):
                continue
            if option.quote_time is not None:
                observed_dates[option.quote_time.date().isoformat()] += 1
            if option.quote_time is None or option.quote_time.date() != as_of:
                continue
            output.append(option)
        if historical_catchup and by_code and not output:
            dates = ", ".join(f"{day}:{count}" for day, count in sorted(observed_dates.items()))
            raise RuntimeError(
                f"no option Market Snapshot rows were timestamped {as_of}; "
                f"observed update dates: {dates or 'none'}")
        return output

    def get_option_regimes(self, codes: list[str], as_of: date) -> dict[str, OptionRegimeSnapshot]:
        """Fetch dated Moomoo underlying IV rank/percentile in one paged call.

        The vendor percentile is retained as a separately labelled source; it
        never populates the locally collected rolling near-ATM history.
        """
        from moomoo import (OptionMarket, UnderlyingRankFilter,  # type: ignore
                            UnderlyingRankIndicatorType, UnderlyingRankSortType)

        normalized_codes = [code if code.startswith("US.") else f"US.{code}"
                            for code in dict.fromkeys(codes)]
        if not normalized_codes:
            return {}
        owner_filter = UnderlyingRankFilter(
            UnderlyingRankIndicatorType.OWNER_LIST,
            security_list=normalized_codes,
        )
        page = None
        output: dict[str, OptionRegimeSnapshot] = {}
        while True:
            result = self._call(
                self._ctx.get_option_underlying_rank,
                option_market=OptionMarket.US_SECURITY,
                sort_type=UnderlyingRankSortType.IV,
                count=min(200, len(normalized_codes)),
                trading_date=as_of.isoformat(),
                filter_list=[owner_filter],
                page=page,
            )
            frame = result[1]
            for row in frame.to_dict("records"):
                snapshot = normalize_option_regime(row, as_of)
                output[snapshot.symbol] = snapshot
            next_page = result[2] if len(result) > 2 else None
            if not next_page or next_page == page:
                break
            page = next_page
        return output

    def screen_us_panic_candidates(self, config) -> list[dict]:
        """High-recall US coarse screen using OpenAPI Stock Screening V2.

        The exact 60-day peak drawdown is deliberately left to the local
        second-stage calculation. The API screen only reduces the universe.
        """
        from moomoo import StockScreenRequest  # type: ignore
        from moomoo.quote.stock_screen_const import (  # type: ignore
            BasicProperty, CumulativeProperty, ScrMarket, ScrSortDir,
            SimpleField, SimpleProperty,
        )

        field_ids = {
            "symbol": int(BasicProperty.CODE), "name": int(BasicProperty.NAME),
            "price": int(SimpleProperty.PRICE), "market_cap": int(SimpleProperty.MARKET_CAP),
            "volume_ratio": int(SimpleProperty.VOLUME_RATIO),
            "change_5d": int(CumulativeProperty.PRICE_CHANGE_PCT),
            "avg_volume_20d": int(CumulativeProperty.AVG_VOLUME),
        }
        output_by_symbol: dict[str, dict] = {}
        page_from = 0
        market = getattr(ScrMarket, "US", None)
        if market is None:
            market = getattr(ScrMarket, "USA", None)
        if market is None:
            raise RuntimeError("installed SDK does not expose the US Stock Screening V2 market enum")
        while len(output_by_symbol) < config.screener_max_results:
            request = StockScreenRequest()
            request.add_simple_field(field=SimpleField.MARKET, values=[market])
            request.add_simple_property(name=SimpleProperty.PRICE, lower=config.screener_min_price)
            request.add_simple_property(name=SimpleProperty.MARKET_CAP, lower=config.screener_min_market_cap)
            request.add_simple_property(name=SimpleProperty.VOLUME_RATIO, lower=config.screener_min_volume_ratio)
            request.add_cumulative_property(name=CumulativeProperty.PRICE_CHANGE_PCT,
                                            days=5, upper=config.screener_max_5d_change)
            request.add_retrieve_basic(name=BasicProperty.CODE)
            request.add_retrieve_basic(name=BasicProperty.NAME)
            request.add_retrieve_simple(name=SimpleProperty.PRICE)
            request.add_retrieve_simple(name=SimpleProperty.MARKET_CAP)
            request.add_retrieve_simple(name=SimpleProperty.VOLUME_RATIO)
            request.add_retrieve_cumulative(name=CumulativeProperty.PRICE_CHANGE_PCT, days=5)
            request.add_retrieve_cumulative(name=CumulativeProperty.AVG_VOLUME, days=20)
            request.set_sort(direction=ScrSortDir.ASC, property_type="cumulative",
                             property_params={"name": int(CumulativeProperty.PRICE_CHANGE_PCT), "days": 5})
            request.page_from = page_from
            request.page_count = min(200, config.screener_max_results - len(output_by_symbol))
            _, data = self._call(self._ctx.get_stock_screen, request)
            last_page, _, items = data
            for item in items:
                row = normalize_stock_screen_item(item, field_ids)
                try:
                    row["symbol"] = str(row["symbol"]).removeprefix("US.").upper()
                    row["avg_dollar_volume_20d"] = float(row["price"]) * float(row["avg_volume_20d"])
                    row["volume_ratio"] = float(row["volume_ratio"])
                    row["change_5d"] = float(row["change_5d"])
                except (TypeError, ValueError):
                    continue
                if row["avg_dollar_volume_20d"] >= config.screener_min_avg_dollar_volume:
                    output_by_symbol[row["symbol"]] = row
                if len(output_by_symbol) >= config.screener_max_results:
                    break
            if last_page or not items:
                break
            page_from += len(items)
        return sorted(output_by_symbol.values(), key=lambda row: float(row["change_5d"]))

    def get_short_interest(self, code: str, start: date, end: date) -> list[ShortSnapshot]:
        try:
            frame = self._call(self._ctx.get_short_interest, code, start=str(start), end=str(end))[1]
        except TypeError:
            frame = self._call(self._ctx.get_short_interest, code)[1]
        rows = frame.to_dict("records")
        return [ShortSnapshot(
            date.fromisoformat(str(_pick(r, "time", "date", "timestamp_str"))[:10]),
            short_interest=float(_pick(r, "short_interest", "short_interest_qty", "short_qty", default=0) or 0),
            short_interest_date=date.fromisoformat(str(_pick(r, "time", "date", "timestamp_str"))[:10]),
        ) for r in rows]

    def get_daily_short_volume(self, code: str, pages: int = 1) -> list[dict]:
        rows: list[dict] = []
        next_key = None
        for _ in range(max(1, pages)):
            kwargs = {"num": 50}
            if next_key not in (None, "", "-1"):
                kwargs["next_key"] = next_key
            result = self._call(self._ctx.get_daily_short_volume, code, **kwargs)
            frame = result[1]
            rows.extend(frame.to_dict("records"))
            next_key = getattr(frame, "attrs", {}).get("next_key", "-1")
            if next_key in (None, "", "-1"):
                break
        return rows

    def get_short_snapshot(self, code: str, start: date, end: date) -> ShortSnapshot | None:
        interest = sorted(self.get_short_interest(code, start, end), key=lambda x: x.day)
        flow = self.get_daily_short_volume(code)
        current_i = interest[-1] if interest else None
        previous_i = interest[-2] if len(interest) >= 2 else None
        flow = sorted(flow, key=lambda r: str(_pick(r, "timestamp_str", "time", "date", default="")))
        current_f = flow[-1] if flow else None
        if not current_i and not current_f:
            return None
        flow_day = (date.fromisoformat(str(_pick(current_f, "timestamp_str", "time", "date"))[:10])
                    if current_f else None)
        day = flow_day or current_i.day
        return ShortSnapshot(
            day=day,
            short_interest=current_i.short_interest if current_i else None,
            short_interest_5d_ago=previous_i.short_interest if previous_i else None,
            short_volume_ratio=(float(_pick(current_f, "short_percent", default=0)) / 100 if current_f else None),
            short_interest_date=current_i.day if current_i else None,
            previous_short_interest_date=previous_i.day if previous_i else None,
            short_volume_ratio_20d_avg=(float(_pick(current_f, "daily_trade_avg_ratio", default=0)) / 100 if current_f else None),
            short_volume_date=flow_day,
        )

    def healthcheck(self) -> dict[str, object]:
        try:
            result = self._call(self._ctx.get_global_state)
            state = result[1]
            server_ver = str(state.get("server_ver") or "") if isinstance(state, dict) else ""
            try:
                server_version_number = int(server_ver)
            except ValueError:
                server_version_number = 0
            compatible = server_version_number >= MIN_OPEND_SERVER_VERSION
            return {
                "ok": compatible,
                "message": (
                    "OpenD reachable"
                    if compatible
                    else (f"OpenD server_ver={server_ver or 'unknown'} is too old; "
                          f"install OpenD {REQUIRED_OPEND_VERSION} to match the SDK and "
                          "support Option Screening V2")
                ),
                "server_ver": server_ver or None,
                "required_opend": REQUIRED_OPEND_VERSION,
                "raw_type": type(state).__name__,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
