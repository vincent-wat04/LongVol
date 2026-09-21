from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

from .models import (Bar, Candidate, FundamentalSnapshot, OptionHistorySnapshot,
                     OptionQuote, OptionRegimeSnapshot, Position, ShortSnapshot)


def _date(x: str) -> date: return date.fromisoformat(x)
def _datetime(x: str) -> datetime: return datetime.fromisoformat(x.replace("Z", "+00:00"))
def _float(row, key, default=0.0): return float(row[key]) if row.get(key, "") not in ("", None) else default


def _bool(row, key: str, default: bool = False) -> bool:
    value = row.get(key, "")
    if value in ("", None):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def read_candidates(path: str | Path) -> list[Candidate]:
    with open(path, newline="") as f:
        return [Candidate(r["symbol"].upper(), _date(r["as_of"]), r.get("screener_reason", ""),
                          {k: float(v) for k, v in r.items() if k.startswith("screen_") and v})
                for r in csv.DictReader(f)]


def read_bars(path: str | Path) -> list[Bar]:
    with open(path, newline="") as f:
        rows = [Bar(_date((r.get("day") or r.get("time") or r.get("time_key") or "")[:10]),
                    _float(r, "open"), _float(r, "high"), _float(r, "low"), _float(r, "close"),
                    _float(r, "volume"),
                    _datetime(r.get("time") or r.get("time_key"))
                    if len(r.get("time") or r.get("time_key") or "") > 10 else None)
                for r in csv.DictReader(f)]
    return sorted(rows, key=lambda x: (x.day, x.timestamp or datetime.combine(x.day, datetime.min.time())))


def read_options(path: str | Path) -> list[OptionQuote]:
    with open(path, newline="") as f:
        return [OptionQuote(
            symbol=r["symbol"], expiry=_date(r["expiry"]), strike=_float(r, "strike"), right=r["right"],
            bid=_float(r, "bid"), ask=_float(r, "ask"), last=_float(r, "last"), iv=_float(r, "iv"),
            delta=_float(r, "delta"), gamma=_float(r, "gamma"), open_interest=_float(r, "open_interest"),
            volume=_float(r, "volume"), iv_percentile=float(r["iv_percentile"]) if r.get("iv_percentile") else None,
            quote_time=_datetime(r["quote_time"]) if r.get("quote_time") else None,
            multiplier=int(float(r.get("multiplier") or 100)), vega=_float(r, "vega", None), theta=_float(r, "theta", None),
        ) for r in csv.DictReader(f)]


def read_short(path: str | Path) -> dict[str, ShortSnapshot]:
    with open(path, newline="") as f:
        return {r["symbol"].upper(): ShortSnapshot(
            day=_date(r["day"]), short_interest=_float(r, "short_interest", None),
            short_interest_5d_ago=_float(r, "short_interest_5d_ago", None),
            short_volume_ratio=_float(r, "short_volume_ratio", None),
            short_volume_ratio_5d_ago=_float(r, "short_volume_ratio_5d_ago", None),
            short_interest_date=_date(r["short_interest_date"]) if r.get("short_interest_date") else None,
            previous_short_interest_date=_date(r["previous_short_interest_date"]) if r.get("previous_short_interest_date") else None,
            short_volume_ratio_20d_avg=_float(r, "short_volume_ratio_20d_avg", None),
            short_volume_date=_date(r["short_volume_date"]) if r.get("short_volume_date") else None,
        ) for r in csv.DictReader(f)}


def read_fundamentals(path: str | Path) -> dict[str, FundamentalSnapshot]:
    with open(path, newline="") as f:
        return {r["symbol"].upper(): FundamentalSnapshot(
            catalyst=r.get("catalyst", ""), catalyst_present=r.get("catalyst_present", "").lower() == "true",
            valuation_regime_break=r.get("valuation_regime_break", "").lower() == "true",
            valuation_reason=r.get("valuation_reason", ""), thesis_status=r.get("thesis_status", "uncertain"),
            evidence=tuple(x for x in r.get("evidence", "").split(" | ") if x),
            confidence=float(r["confidence"]) if r.get("confidence") else None,
            as_of=_date(r["as_of"]) if r.get("as_of") else None,
            needs_review=r.get("needs_review", "").lower() == "true",
            sources=tuple(x for x in r.get("sources", "").split(" | ") if x),
            event_class=r.get("event_class", "UNKNOWN") or "UNKNOWN",
            selloff_explanation=r.get("selloff_explanation", ""),
            material_news_found=_bool(r, "material_news_found"),
            source_ids=tuple(x for x in r.get("source_ids", "").split(" | ") if x),
            news_packet_sha256=r.get("news_packet_sha256", ""),
            retrieval_mode=r.get("retrieval_mode", "NO_VERIFIED_RETRIEVAL") or
            "NO_VERIFIED_RETRIEVAL",
            point_in_time_collection_ok=_bool(r, "point_in_time_collection_ok"),
            native_web_search_requested=_bool(r, "native_web_search_requested"),
            native_web_search_observed=_bool(r, "native_web_search_observed"),
            research_request_id=r.get("research_request_id", ""),
        ) for r in csv.DictReader(f)}


def read_option_history(path: str | Path) -> dict[str, list[OptionHistorySnapshot]]:
    result: dict[str, list[OptionHistorySnapshot]] = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            result.setdefault(r["symbol"].upper(), []).append(OptionHistorySnapshot(r["symbol"].upper(), _date(r["day"]), float(r["near_atm_iv"]) if r.get("near_atm_iv") else None))
    for values in result.values():
        values.sort(key=lambda x: x.day)
    return result


def read_option_regimes(path: str | Path) -> dict[str, OptionRegimeSnapshot]:
    """Read the latest point-in-time Moomoo option regime per symbol."""
    result: dict[str, OptionRegimeSnapshot] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            symbol = row["symbol"].upper()
            snapshot = OptionRegimeSnapshot(
                symbol=symbol,
                day=_date(row["day"]),
                iv=_float(row, "iv", None),
                iv_rank=_float(row, "iv_rank", None),
                iv_percentile=_float(row, "iv_percentile", None),
                iv_change=_float(row, "iv_change", None),
                hv=_float(row, "hv", None),
                hv_change=_float(row, "hv_change", None),
                put_call_volume_ratio=_float(row, "put_call_volume_ratio", None),
                put_call_open_interest_ratio=_float(
                    row, "put_call_open_interest_ratio", None),
                source=row.get("source") or "moomoo_option_underlying_rank",
            )
            if symbol not in result or snapshot.day > result[symbol].day:
                result[symbol] = snapshot
    return result


def read_positions(path: str | Path) -> list[Position]:
    with open(path, newline="") as f:
        positions = [Position(
            symbol=r["symbol"].upper(), option_symbol=r["option_symbol"], entry_date=_date(r["entry_date"]),
            entry_price=_float(r, "entry_price"), entry_spot=_float(r, "entry_spot"), contracts=int(r["contracts"]),
            risk_per_contract=_float(r, "risk_per_contract"), expiry=_date(r["expiry"]), thesis=r.get("thesis", ""),
            current_option_price=_float(r, "current_option_price", None), entry_atr=_float(r, "entry_atr", None),
            hard_stop_spot=_float(r, "hard_stop_spot", None), option_stop_price=_float(r, "option_stop_price", None),
            target_spot=_float(r, "target_spot", None), target_option_price=_float(r, "target_option_price", None),
            target_source=r.get("target_source", ""), entry_iv=_float(r, "entry_iv", None),
            entry_iv_percentile=_float(r, "entry_iv_percentile", None),
            max_hold_days=int(float(r.get("max_hold_days") or 30)), max_option_price=_float(r, "max_option_price", None),
            status=r.get("status", "OPEN"), multiplier=int(float(r.get("multiplier") or 100)),
            currency=r.get("currency", "USD"),
            capital_at_risk_per_contract=_float(
                r, "capital_at_risk_per_contract", None),
            partial_exit_taken=_bool(r, "partial_exit_taken"),
        ) for r in csv.DictReader(f)]
    for position in positions:
        capital_risk = position.capital_at_risk_per_contract
        if (capital_risk is not None and
                (not math.isfinite(capital_risk) or
                 capital_risk + 1e-9 < position.entry_price * position.multiplier)):
            raise ValueError(
                f"{position.option_symbol} capital_at_risk_per_contract "
                "cannot understate paid premium")
    return positions


def write_rows(path: str | Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    keys = fieldnames or (list(dict.fromkeys(k for row in rows for k in row)) if rows else [])
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            if keys:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader(); writer.writerows(rows)
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: str | Path, value: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False, default=str)
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: str | Path) -> dict:
    with open(path) as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def merge_fundamental(path: str | Path, symbol: str, result: dict, as_of: str | None = None) -> None:
    p = Path(path)
    rows = []
    if p.exists():
        with open(p, newline="") as f:
            rows = list(csv.DictReader(f))
    row = {"symbol": symbol.upper(), "catalyst": result.get("catalyst", ""),
           "catalyst_present": str(bool(result.get("catalyst_present"))).lower(),
           "valuation_regime_break": str(bool(result.get("valuation_regime_break"))).lower(),
           "valuation_reason": result.get("valuation_reason", ""),
           "thesis_status": result.get("thesis_status", "uncertain"),
           "evidence": " | ".join(result.get("evidence", [])),
           "sources": " | ".join(result.get("sources", [])),
           "confidence": result.get("confidence", ""),
           "needs_review": str(bool(result.get("needs_review"))).lower(),
           "event_class": result.get("event_class", "UNKNOWN"),
           "selloff_explanation": result.get("selloff_explanation", ""),
           "material_news_found": str(bool(result.get("material_news_found"))).lower(),
           "source_ids": " | ".join(result.get("source_ids", [])),
           "news_packet_sha256": result.get("news_packet_sha256", ""),
           "retrieval_mode": result.get("retrieval_mode", "NO_VERIFIED_RETRIEVAL"),
           "point_in_time_collection_ok": str(
               bool(result.get("point_in_time_collection_ok"))).lower(),
           "native_web_search_requested": str(
               bool(result.get("native_web_search_requested"))).lower(),
           "native_web_search_observed": str(
               bool(result.get("native_web_search_observed"))).lower(),
           "research_request_id": result.get("request_id", result.get("research_request_id", "")),
           "as_of": as_of or result.get("as_of", "")}
    by_symbol = {r.get("symbol", "").upper(): r for r in rows}
    by_symbol[row["symbol"]] = row
    write_rows(p, list(by_symbol.values()))
