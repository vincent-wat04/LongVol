from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from datetime import date, datetime
from dataclasses import asdict, fields
from pathlib import Path

from .models import Config


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
OPTION_MC_MIN_VALIDATION_OBSERVATION_DAYS = 252
OPTION_MC_MIN_VALIDATION_INDEPENDENT_EVENTS = 100
OPTION_MC_EVIDENCE_SCHEMA_VERSION = 1
OPTION_MC_EVIDENCE_INTENDED_USE = "OPTION_CONTRACT_SELECTION_ONLY"

_OPTION_MC_EVIDENCE_FIELDS = {
    "schema_version",
    "approved",
    "out_of_sample",
    "strategy_version",
    "assumption_source",
    "intended_use",
    "spec_sha256",
    "implementation_sha256",
    "sample_start",
    "feature_end",
    "label_end",
    "parameter_frozen_at",
    "approved_at",
    "observation_days",
    "independent_events",
    "parameter_trials",
    "validation_folds",
    "purge_trading_days",
    "embargo_trading_days",
}

# These files jointly determine the contract simulation, target/stop inputs,
# option ranking, and the sizing R denominator.  Including the Python runtime
# version also prevents a random-number/runtime upgrade from silently reusing
# an old reproducibility claim.
_OPTION_MC_IMPLEMENTATION_FILES = (
    "models.py",
    "data_quality.py",
    "metrics.py",
    "pricing.py",
    "option_simulation.py",
    "strategy.py",
    "kelly.py",
)


def _is_option_mc_evidence_config_field(name: str) -> bool:
    return (name.startswith("option_mc_validation_") or
            name.startswith("option_mc_min_validation_"))


def option_mc_spec_payload(config: Config) -> dict:
    """Return the frozen, non-evidence strategy specification.

    Hashing every non-evidence Config field is deliberately stricter than a
    hand-maintained MC-only allowlist: a future target, execution, or gate
    parameter cannot begin affecting contract selection without invalidating
    the old evidence.
    """

    return {
        field.name: getattr(config, field.name)
        for field in fields(Config)
        if not _is_option_mc_evidence_config_field(field.name)
    }


def option_mc_spec_sha256(config: Config) -> str:
    raw = json.dumps(
        option_mc_spec_payload(config), sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def option_mc_implementation_sha256() -> str:
    package_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    runtime = f"{sys.implementation.name}:{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    digest.update(runtime.encode("utf-8"))
    digest.update(b"\0")
    for name in _OPTION_MC_IMPLEMENTATION_FILES:
        path = package_dir / name
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot hash option MC implementation file: {name}") from exc
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _strict_json_object(payload: bytes) -> dict:
    def reject_duplicate_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("option MC evidence manifest must be strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("option MC evidence manifest must be a JSON object")
    missing = sorted(_OPTION_MC_EVIDENCE_FIELDS - set(value))
    unknown = sorted(set(value) - _OPTION_MC_EVIDENCE_FIELDS)
    if missing or unknown:
        detail = []
        if missing:
            detail.append("missing: " + ", ".join(missing))
        if unknown:
            detail.append("unknown: " + ", ".join(unknown))
        raise ValueError("option MC evidence manifest schema mismatch (" + "; ".join(detail) + ")")
    return value


def _manifest_date(manifest: dict, name: str) -> date:
    value = manifest.get(name)
    if not isinstance(value, str):
        raise ValueError(f"option MC evidence {name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"option MC evidence {name} must be an ISO date") from exc


def _manifest_datetime(manifest: dict, name: str) -> datetime:
    value = manifest.get(name)
    if not isinstance(value, str):
        raise ValueError(f"option MC evidence {name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"option MC evidence {name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"option MC evidence {name} must include a timezone")
    return parsed


def _manifest_int(manifest: dict, name: str, minimum: int) -> int:
    value = manifest.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"option MC evidence {name} must be an integer >= {minimum}")
    return value


def validate_option_mc_evidence(
    config: Config,
    evidence_base_dir: str | Path = Path("."),
    *,
    as_of: date | None = None,
) -> dict | None:
    """Validate the complete OOS manifest against the deployed specification.

    The file hash proves which manifest was approved; the manifest's spec and
    implementation hashes prove that the approval applies to this exact
    strategy, simulator, and runtime.  This function is intentionally callable
    both from config loading and direct ``Config`` strategy API paths.
    """

    if config.option_mc_mode != "OOS_GATE":
        return None
    if config.option_mc_enabled is not True:
        raise ValueError("OOS_GATE requires option_mc_enabled")
    if config.option_mc_out_of_sample_validated is not True:
        raise ValueError("OOS_GATE requires out-of-sample validated assumptions")
    source = config.option_mc_assumption_source
    if (not isinstance(source, str) or not source.strip() or
            source.strip().upper() == "UNVALIDATED_PRIOR"):
        raise ValueError("OOS_GATE requires a non-prior assumption source")
    if (not isinstance(config.option_mc_min_validation_observation_days, int) or
            isinstance(config.option_mc_min_validation_observation_days, bool) or
            config.option_mc_min_validation_observation_days <
            OPTION_MC_MIN_VALIDATION_OBSERVATION_DAYS):
        raise ValueError("option MC observation-day governance minimum cannot be lowered")
    if (not isinstance(config.option_mc_min_validation_independent_events, int) or
            isinstance(config.option_mc_min_validation_independent_events, bool) or
            config.option_mc_min_validation_independent_events <
            OPTION_MC_MIN_VALIDATION_INDEPENDENT_EVENTS):
        raise ValueError("option MC independent-event governance minimum cannot be lowered")

    study_path = config.option_mc_validation_study_path
    if not isinstance(study_path, str) or not study_path.strip():
        raise ValueError("OOS_GATE requires option_mc_validation_study_path")
    resolved_study = Path(study_path)
    if not resolved_study.is_absolute():
        resolved_study = Path(evidence_base_dir) / resolved_study
    resolved_study = resolved_study.resolve()
    if not resolved_study.is_file():
        raise ValueError("OOS_GATE validation study file does not exist")
    try:
        manifest_bytes = resolved_study.read_bytes()
    except OSError as exc:
        raise ValueError("OOS_GATE validation study file cannot be read") from exc

    expected_file_hash = config.option_mc_validation_study_sha256
    if not isinstance(expected_file_hash, str) or not _SHA256.fullmatch(expected_file_hash):
        raise ValueError("OOS_GATE requires a valid study SHA-256")
    observed_file_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if observed_file_hash.lower() != expected_file_hash.lower():
        raise ValueError("OOS_GATE validation study SHA-256 mismatch")

    manifest = _strict_json_object(manifest_bytes)
    if (not isinstance(manifest["schema_version"], int) or
            isinstance(manifest["schema_version"], bool) or
            manifest["schema_version"] != OPTION_MC_EVIDENCE_SCHEMA_VERSION):
        raise ValueError("unsupported option MC evidence schema_version")
    if manifest["approved"] is not True or manifest["out_of_sample"] is not True:
        raise ValueError("option MC evidence must be approved and out_of_sample")
    if manifest["strategy_version"] != config.strategy_version:
        raise ValueError("option MC evidence strategy_version mismatch")
    if manifest["assumption_source"] != source:
        raise ValueError("option MC evidence assumption_source mismatch")
    if manifest["intended_use"] != OPTION_MC_EVIDENCE_INTENDED_USE:
        raise ValueError("option MC evidence intended_use mismatch")

    manifest_spec_hash = manifest["spec_sha256"]
    if (not isinstance(manifest_spec_hash, str) or
            not _SHA256.fullmatch(manifest_spec_hash) or
            manifest_spec_hash.lower() != option_mc_spec_sha256(config)):
        raise ValueError("option MC evidence spec SHA-256 mismatch")
    manifest_implementation_hash = manifest["implementation_sha256"]
    if (not isinstance(manifest_implementation_hash, str) or
            not _SHA256.fullmatch(manifest_implementation_hash) or
            manifest_implementation_hash.lower() != option_mc_implementation_sha256()):
        raise ValueError("option MC evidence implementation SHA-256 mismatch")

    sample_start = _manifest_date(manifest, "sample_start")
    feature_end = _manifest_date(manifest, "feature_end")
    label_end = _manifest_date(manifest, "label_end")
    if not sample_start <= feature_end < label_end:
        raise ValueError("option MC evidence dates must satisfy sample_start <= feature_end < label_end")
    configured_sample_end = config.option_mc_validation_sample_end
    if not isinstance(configured_sample_end, str):
        raise ValueError("OOS_GATE requires a valid validation sample end")
    try:
        sample_end = date.fromisoformat(configured_sample_end)
    except ValueError as exc:
        raise ValueError("OOS_GATE requires a valid validation sample end") from exc
    if sample_end != label_end:
        raise ValueError("option MC evidence label_end/sample_end mismatch")

    parameter_frozen_at = _manifest_datetime(manifest, "parameter_frozen_at")
    approved_at = _manifest_datetime(manifest, "approved_at")
    if parameter_frozen_at > approved_at:
        raise ValueError("option MC parameters must be frozen before approval")
    if parameter_frozen_at.date() >= sample_start:
        raise ValueError("option MC parameters must be frozen before the OOS sample starts")
    if approved_at.date() <= label_end:
        raise ValueError("option MC evidence cannot be approved before labels are complete")
    if as_of is not None:
        if label_end >= as_of or sample_end >= as_of:
            raise ValueError("OOS_GATE label_end/sample_end must be strictly before the signal date")
        if approved_at.date() >= as_of:
            raise ValueError("OOS_GATE approval must be strictly before the signal date")

    observation_days = _manifest_int(
        manifest, "observation_days", OPTION_MC_MIN_VALIDATION_OBSERVATION_DAYS,
    )
    independent_events = _manifest_int(
        manifest, "independent_events", OPTION_MC_MIN_VALIDATION_INDEPENDENT_EVENTS,
    )
    if observation_days != config.option_mc_validation_observation_days:
        raise ValueError("option MC evidence observation_days mismatch")
    if independent_events != config.option_mc_validation_independent_events:
        raise ValueError("option MC evidence independent_events mismatch")
    if observation_days < config.option_mc_min_validation_observation_days:
        raise ValueError("OOS_GATE validation observation days are insufficient")
    if independent_events < config.option_mc_min_validation_independent_events:
        raise ValueError("OOS_GATE validation independent events are insufficient")
    if (feature_end - sample_start).days + 1 < observation_days:
        raise ValueError("option MC evidence date span cannot contain the claimed observation_days")

    _manifest_int(manifest, "parameter_trials", 1)
    validation_folds = _manifest_int(manifest, "validation_folds", 2)
    if validation_folds > independent_events:
        raise ValueError("option MC validation_folds exceed independent_events")
    purge = _manifest_int(manifest, "purge_trading_days", 0)
    embargo = _manifest_int(manifest, "embargo_trading_days", 0)
    if purge < config.option_mc_horizon_trading_days:
        raise ValueError("option MC purge must cover the simulation horizon")
    if embargo < config.option_mc_horizon_trading_days:
        raise ValueError("option MC embargo must cover the simulation horizon")
    return manifest


def load_config(path: str | Path | None) -> Config:
    """Load a strict JSON strategy configuration.

    Unknown keys fail immediately. Silent configuration drift is more
    dangerous than a failed run in a trading workflow.
    """
    if path is None:
        config = Config()
        _validate(config, Path("."))
        return config
    config_path = Path(path).resolve()
    value = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("strategy config must be a JSON object")
    allowed = {f.name for f in fields(Config)}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown strategy config keys: {', '.join(unknown)}")
    evidence_path = value.get("option_mc_validation_study_path")
    if evidence_path not in (None, ""):
        if not isinstance(evidence_path, str):
            raise ValueError("option_mc_validation_study_path must be a string")
        resolved_evidence = Path(evidence_path)
        if not resolved_evidence.is_absolute():
            resolved_evidence = config_path.parent / resolved_evidence
        # Store the canonical path on the frozen Config so later evaluate()
        # calls never reinterpret it relative to a different process cwd.
        value["option_mc_validation_study_path"] = str(resolved_evidence.resolve())
    config = Config(**value)
    _validate(config, config_path.parent)
    return config


def _validate(config: Config, evidence_base_dir: Path = Path(".")) -> None:
    if not 0 < config.profile_value_area_pct <= 1:
        raise ValueError("profile_value_area_pct must be in (0, 1]")
    if not 0 < config.target_delta_low < config.target_delta_high <= 1:
        raise ValueError("target delta range is invalid")
    if not 0 < config.min_dte < config.max_dte:
        raise ValueError("DTE range is invalid")
    if config.forced_expiry_exit_dte >= config.min_dte_exit:
        raise ValueError("forced_expiry_exit_dte must be below min_dte_exit")
    for name in ("max_spread_pct", "max_premium_to_spot", "option_premium_stop_pct",
                 "max_iv_percentile", "max_iv_crush_pct", "gamma_wall_min_abs_gex_share",
                 "max_portfolio_premium_allocation", "max_portfolio_risk_allocation"):
        value = getattr(config, name)
        if not 0 < value < 1:
            raise ValueError(f"{name} must be in (0, 1)")
    if config.stop_atr_multiple <= 0 or config.take_profit_r <= 0:
        raise ValueError("stop and profit multiples must be positive")
    if (isinstance(config.option_fee_per_contract, bool) or
            not isinstance(config.option_fee_per_contract, (int, float)) or
            not math.isfinite(float(config.option_fee_per_contract)) or
            config.option_fee_per_contract < 0):
        raise ValueError("option_fee_per_contract must be a finite non-negative number per side")
    if config.min_term_structure_slope >= config.max_term_structure_slope:
        raise ValueError("term structure slope range is invalid")
    if (not isinstance(config.require_iv_regime_gate, bool) or
            not isinstance(config.require_option_surface_gate, bool)):
        raise ValueError("IV regime and option surface gate flags must be boolean")
    if config.max_market_residual_5d >= 0 or config.max_sector_residual_5d >= 0:
        raise ValueError("residual-shock thresholds must be negative returns")
    if config.max_intraday_calendar_days < 7:
        raise ValueError("max_intraday_calendar_days must be at least 7")
    if config.profile_min_bars <= 0 or config.max_holding_days <= 0:
        raise ValueError("profile_min_bars and max_holding_days must be positive")
    if (config.screener_min_price <= 0 or config.screener_min_market_cap <= 0 or
            config.screener_min_avg_dollar_volume <= 0 or not 1 <= config.screener_max_results <= 1000):
        raise ValueError("screener thresholds are invalid")
    if config.tracked_max_age_days <= 0:
        raise ValueError("tracked_max_age_days must be positive")
    for name in ("min_atr_compression", "min_rv_compression",
                 "min_volume_compression"):
        value = getattr(config, name)
        if not 0 < value <= 1:
            raise ValueError(f"{name} must be in (0, 1]")
    # panic_metrics intentionally searches the latest five bars, leaving at
    # most four completed post-shock sessions in the active event window.
    if not 1 <= config.min_post_shock_sessions <= 4:
        raise ValueError("min_post_shock_sessions must be between 1 and 4")
    if (config.option_surface_min_open_interest < 0 or config.option_flow_min_volume < 0 or
            config.option_screener_max_contracts < 200):
        raise ValueError("option screener limits are invalid")
    if config.min_history_bars < 60 or config.min_iv_history_observations < 20:
        raise ValueError("history requirements are too short for the configured features")
    if (config.max_open_positions <= 0 or config.max_positions_per_underlying <= 0 or
            config.max_positions_per_underlying > config.max_open_positions):
        raise ValueError("portfolio position limits are invalid")
    if (not isinstance(config.option_mc_mode, str) or
            config.option_mc_mode not in {"RESEARCH_ONLY", "OOS_GATE"}):
        raise ValueError("option_mc_mode must be RESEARCH_ONLY or OOS_GATE")
    if not isinstance(config.option_mc_enabled, bool):
        raise ValueError("option_mc_enabled must be boolean")
    if not isinstance(config.option_mc_out_of_sample_validated, bool):
        raise ValueError("option_mc_out_of_sample_validated must be boolean")
    if (not isinstance(config.option_mc_paths, int) or
            isinstance(config.option_mc_paths, bool)):
        raise ValueError("option_mc_paths must be an integer")
    if not 100 <= config.option_mc_paths <= 1_000_000:
        raise ValueError("option_mc_paths must be between 100 and 1000000")
    if (not isinstance(config.option_mc_seed, int) or isinstance(config.option_mc_seed, bool)):
        raise ValueError("option_mc_seed must be an integer")
    if (not isinstance(config.option_mc_horizon_trading_days, int) or
            isinstance(config.option_mc_horizon_trading_days, bool)):
        raise ValueError("option_mc_horizon_trading_days must be an integer")
    if not 1 <= config.option_mc_horizon_trading_days <= config.max_holding_days:
        raise ValueError("option_mc_horizon_trading_days must be within max_holding_days")
    numeric_mc_fields = (
        "option_mc_rebound_probability", "option_mc_continuation_probability",
        "option_mc_min_expected_return", "option_mc_min_probability_profit",
        "option_mc_min_target_scenario_return", "option_mc_min_robust_score",
        "option_mc_max_cvar_capital_fraction", "option_mc_min_target_payoff_to_loss",
    )
    for name in numeric_mc_fields:
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{name} must be a finite number")
    if (not 0 <= config.option_mc_rebound_probability <= 1 or
            not 0 <= config.option_mc_continuation_probability <= 1 or
            config.option_mc_rebound_probability + config.option_mc_continuation_probability > 1):
        raise ValueError("option MC state probabilities are invalid")
    if (not isinstance(config.option_mc_assumption_source, str) or
            not config.option_mc_assumption_source.strip()):
        raise ValueError("option_mc_assumption_source is required")
    if (config.option_mc_out_of_sample_validated and
            config.option_mc_assumption_source.strip().upper() == "UNVALIDATED_PRIOR"):
        raise ValueError("validated option MC assumptions require a non-prior source")
    for name in (
        "option_mc_validation_observation_days",
        "option_mc_validation_independent_events",
        "option_mc_min_validation_observation_days",
        "option_mc_min_validation_independent_events",
    ):
        value = getattr(config, name)
        if (not isinstance(value, int) or isinstance(value, bool) or
                value < (1 if name.startswith("option_mc_min_") else 0)):
            raise ValueError(f"{name} must be a valid non-negative integer")
    if (config.option_mc_min_validation_observation_days <
            OPTION_MC_MIN_VALIDATION_OBSERVATION_DAYS):
        raise ValueError("option MC observation-day governance minimum cannot be lowered")
    if (config.option_mc_min_validation_independent_events <
            OPTION_MC_MIN_VALIDATION_INDEPENDENT_EVENTS):
        raise ValueError("option MC independent-event governance minimum cannot be lowered")
    if config.option_mc_mode == "OOS_GATE":
        validate_option_mc_evidence(config, evidence_base_dir)
    if not 0 <= config.option_mc_min_probability_profit <= 1:
        raise ValueError("option_mc_min_probability_profit must be in [0, 1]")
    if not -1 <= config.option_mc_min_expected_return:
        raise ValueError("option_mc_min_expected_return must be at least -1")
    if not -1 <= config.option_mc_min_target_scenario_return:
        raise ValueError("option_mc_min_target_scenario_return must be at least -1")
    if not 0 <= config.option_mc_max_cvar_capital_fraction <= 1:
        raise ValueError("option_mc_max_cvar_capital_fraction must be in [0, 1]")
    if config.option_mc_min_target_payoff_to_loss < 0:
        raise ValueError("option_mc_min_target_payoff_to_loss must be non-negative")


def config_payload(config: Config) -> dict:
    return asdict(config)


def config_sha256(config: Config) -> str:
    raw = json.dumps(config_payload(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
