from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .kelly import FixedRiskInput, KellyInput


AUTO = "AUTO"
COLD_START = "COLD_START_FIXED_RISK"
VALIDATED = "VALIDATED_KELLY"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
MIN_KELLY_OOS_OBSERVATION_DAYS = 252
MIN_KELLY_COMPLETED_TRADES = 100
MIN_KELLY_INDEPENDENT_EVENTS = 100


@dataclass(frozen=True)
class SizingResolution:
    mode: str
    sizing: KellyInput | FixedRiskInput | None
    validated_ready: bool
    blockers: tuple[str, ...]
    evidence: dict


def _positive_number(value, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _nonnegative_number(value, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _limits(raw: dict, prefix: str) -> tuple[float, float, int]:
    risk = _positive_number(raw.get("max_risk_allocation"), f"{prefix}.max_risk_allocation")
    premium = _positive_number(raw.get("max_premium_allocation"), f"{prefix}.max_premium_allocation")
    raw_contracts = _positive_number(raw.get("max_contracts"), f"{prefix}.max_contracts")
    if not raw_contracts.is_integer():
        raise ValueError(f"{prefix}.max_contracts must be a whole number")
    contracts = int(raw_contracts)
    if not 0 < risk < 1 or not 0 < premium < 1:
        raise ValueError(f"{prefix} allocation limits must be below 1")
    return risk, premium, contracts


def resolve_sizing(
    payload: dict,
    *,
    equity: float | None,
    entry_price: float | None,
    target_price: float | None,
    stop_price: float | None,
    multiplier: int,
    strategy_version: str,
    local_iv_observations: int,
    min_local_iv_observations: int,
    as_of: date,
    evidence_base_dir: str | Path = ".",
) -> SizingResolution:
    """Resolve AUTO sizing without treating missing research as a default.

    Automatic transition to Kelly requires both sufficient locally collected
    IV observations and an explicit, version-matched, out-of-sample evidence
    record. A newly collected sample is never self-approved by this function.
    """
    unknown_top = sorted(set(payload) - {"mode", "cold_start", "validated_kelly"})
    if unknown_top:
        raise ValueError(f"unknown sizing config keys: {', '.join(unknown_top)}")
    if payload.get("mode") != AUTO:
        raise ValueError("sizing config mode must be AUTO")
    cold = payload.get("cold_start")
    validated = payload.get("validated_kelly")
    if not isinstance(cold, dict) or not isinstance(validated, dict):
        raise ValueError("sizing config requires cold_start and validated_kelly objects")
    evidence = validated.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("validated_kelly.evidence must be an object")
    cold_allowed = {
        "fees_per_contract", "max_risk_allocation", "max_premium_allocation",
        "max_contracts", "max_portfolio_premium_allocation",
        "max_portfolio_risk_allocation", "max_open_positions",
        "max_positions_per_underlying",
    }
    validated_allowed = {
        "win_probability_lower_bound", "fees_per_contract", "fractional_kelly",
        "max_risk_allocation", "max_premium_allocation", "max_contracts",
        "max_portfolio_premium_allocation", "max_portfolio_risk_allocation",
        "max_open_positions", "max_positions_per_underlying",
        "min_oos_observation_days", "min_oos_completed_trades",
        "min_oos_independent_events", "evidence",
    }
    evidence_allowed = {
        "approved", "out_of_sample", "strategy_version", "study_path",
        "study_sha256", "sample_end", "observation_days", "completed_trades",
        "independent_events",
    }
    for name, raw, allowed in (
        ("cold_start", cold, cold_allowed),
        ("validated_kelly", validated, validated_allowed),
        ("validated_kelly.evidence", evidence, evidence_allowed),
    ):
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"unknown {name} keys: {', '.join(unknown)}")

    # Validate both branches on every run. A malformed future Kelly branch
    # must fail during cold start instead of surfacing only on transition day.
    cold_limits = _limits(cold, "cold_start")
    validated_limits = _limits(validated, "validated_kelly")
    for name, raw in (("cold_start", cold), ("validated_kelly", validated)):
        _nonnegative_number(raw.get("fees_per_contract", 0),
                            f"{name}.fees_per_contract")
        portfolio_limits(payload, COLD_START if name == "cold_start" else VALIDATED)
    fraction = _positive_number(
        validated.get("fractional_kelly"),
        "validated_kelly.fractional_kelly",
    )
    if fraction > 1:
        raise ValueError("validated_kelly.fractional_kelly must be at most 1")

    blockers: list[str] = []
    if local_iv_observations < min_local_iv_observations:
        blockers.append("LOCAL_IV_HISTORY_INCOMPLETE")
    probability = validated.get("win_probability_lower_bound")
    try:
        probability_value = float(probability)
    except (TypeError, ValueError):
        probability_value = 0.0
    if not 0 < probability_value < 1:
        blockers.append("WIN_PROBABILITY_LOWER_BOUND_MISSING")
    if evidence.get("approved") is not True:
        blockers.append("EVIDENCE_NOT_APPROVED")
    if evidence.get("out_of_sample") is not True:
        blockers.append("EVIDENCE_NOT_OUT_OF_SAMPLE")
    if str(evidence.get("strategy_version") or "") != strategy_version:
        blockers.append("EVIDENCE_VERSION_MISMATCH")
    expected_hash = str(evidence.get("study_sha256") or "")
    if not _SHA256.fullmatch(expected_hash):
        blockers.append("EVIDENCE_HASH_MISSING")
    study_path = str(evidence.get("study_path") or "")
    if not study_path:
        blockers.append("EVIDENCE_FILE_MISSING")
    else:
        path = Path(study_path)
        if not path.is_absolute():
            path = Path(evidence_base_dir) / path
        if not path.is_file():
            blockers.append("EVIDENCE_FILE_MISSING")
        elif _SHA256.fullmatch(expected_hash):
            observed_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if observed_hash.lower() != expected_hash.lower():
                blockers.append("EVIDENCE_HASH_MISMATCH")
    try:
        sample_end = date.fromisoformat(str(evidence.get("sample_end") or ""))
        if sample_end >= as_of:
            blockers.append("EVIDENCE_SAMPLE_END_NOT_BEFORE_SIGNAL")
    except ValueError:
        blockers.append("EVIDENCE_SAMPLE_END_INVALID")
    try:
        observation_days = int(evidence.get("observation_days") or 0)
        completed_trades = int(evidence.get("completed_trades") or 0)
        independent_events = int(evidence.get("independent_events") or 0)
        min_days = int(validated.get("min_oos_observation_days") or 0)
        min_trades = int(validated.get("min_oos_completed_trades") or 0)
        min_events = int(validated.get("min_oos_independent_events") or 0)
        if (min_days < MIN_KELLY_OOS_OBSERVATION_DAYS or
                min_trades < MIN_KELLY_COMPLETED_TRADES or
                min_events < MIN_KELLY_INDEPENDENT_EVENTS):
            blockers.append("OOS_GOVERNANCE_MINIMUM_TOO_LOW")
        if min_days <= 0 or observation_days < min_days:
            blockers.append("OOS_OBSERVATION_DAYS_INSUFFICIENT")
        if min_trades <= 0 or completed_trades < min_trades:
            blockers.append("OOS_COMPLETED_TRADES_INSUFFICIENT")
        if min_events <= 0 or independent_events < min_events:
            blockers.append("OOS_INDEPENDENT_EVENTS_INSUFFICIENT")
    except (TypeError, ValueError):
        blockers.append("EVIDENCE_SAMPLE_COUNTS_INVALID")

    common_missing = any(value in (None, "") for value in (
        equity, entry_price, target_price, stop_price,
    ))
    if common_missing:
        return SizingResolution("UNAVAILABLE", None, False,
                                tuple(dict.fromkeys([*blockers, "SIZING_INPUTS_MISSING"])), evidence)

    if not blockers:
        risk, premium, max_contracts = validated_limits
        sizing = KellyInput(
            equity=float(equity), win_probability=probability_value,
            entry_price=float(entry_price), target_price=float(target_price),
            stop_price=float(stop_price),
            fees_per_contract=float(validated.get("fees_per_contract", 0)),
            multiplier=int(multiplier),
            fractional_kelly=fraction,
            max_allocation=risk, max_premium_allocation=premium,
            max_contracts=max_contracts,
        )
        return SizingResolution(VALIDATED, sizing, True, (), evidence)

    risk, premium, max_contracts = cold_limits
    sizing = FixedRiskInput(
        equity=float(equity), entry_price=float(entry_price),
        target_price=float(target_price), stop_price=float(stop_price),
        fees_per_contract=float(cold.get("fees_per_contract", 0)),
        multiplier=int(multiplier), max_risk_allocation=risk,
        max_premium_allocation=premium, max_contracts=max_contracts,
    )
    return SizingResolution(COLD_START, sizing, False,
                            tuple(dict.fromkeys(blockers)), evidence)


def portfolio_limits(payload: dict, mode: str) -> tuple[float, float, int, int]:
    key = "cold_start" if mode == COLD_START else "validated_kelly"
    raw = payload.get(key)
    if not isinstance(raw, dict):
        raise ValueError(f"sizing config requires {key}")
    premium = _positive_number(raw.get("max_portfolio_premium_allocation"),
                               f"{key}.max_portfolio_premium_allocation")
    risk = _positive_number(raw.get("max_portfolio_risk_allocation"),
                            f"{key}.max_portfolio_risk_allocation")
    raw_positions = _positive_number(raw.get("max_open_positions"),
                                     f"{key}.max_open_positions")
    raw_per_underlying = _positive_number(raw.get("max_positions_per_underlying"),
                                          f"{key}.max_positions_per_underlying")
    if not raw_positions.is_integer() or not raw_per_underlying.is_integer():
        raise ValueError(f"{key} position limits must be whole numbers")
    positions = int(raw_positions)
    per_underlying = int(raw_per_underlying)
    if not 0 < premium < 1 or not 0 < risk < 1 or per_underlying > positions:
        raise ValueError(f"{key} portfolio limits are invalid")
    return premium, risk, positions, per_underlying
