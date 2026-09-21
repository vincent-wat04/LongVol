from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from dataclasses import asdict, replace
from datetime import date, timedelta
from datetime import datetime
from functools import wraps
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

from . import __version__
from .config import config_payload, config_sha256, load_config
from .exits import evaluate_exit
from .io import (read_bars, read_candidates, read_fundamentals, read_json, read_option_history,
                 read_option_regimes, read_options, read_positions, read_short, write_rows,
                 merge_fundamental, write_json)
from .models import Candidate, Evaluation, Position
from .option_snapshots import archive_option_snapshot, load_option_snapshot
from .readiness import portfolio_limits, resolve_sizing
from .session import account_snapshot_matches_session
from .strategy import evaluate
from .trading_log import TradingLog


def _new_runs_fail_closed(run_type: str):
    """Mark runs created by a failed command instead of leaving STARTED rows."""
    def decorate(function):
        @wraps(function)
        def wrapped(args):
            log_path = getattr(args, "log_db", None)
            before: set[str] = set()
            if log_path and Path(log_path).exists():
                database = sqlite3.connect(log_path)
                try:
                    has_runs = database.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
                    ).fetchone()
                    if has_runs:
                        before = {str(row[0]) for row in database.execute(
                            "SELECT run_id FROM runs WHERE run_type=? AND status='STARTED'",
                            (run_type,),
                        )}
                finally:
                    database.close()
            try:
                return function(args)
            except Exception as exc:
                if log_path and Path(log_path).exists():
                    failure_log = TradingLog(log_path)
                    try:
                        pending = [str(row[0]) for row in failure_log.db.execute(
                            "SELECT run_id FROM runs WHERE run_type=? AND status='STARTED'",
                            (run_type,),
                        ) if str(row[0]) not in before]
                        for run_id in pending:
                            failure_log.finish_run(run_id, "FAILED", str(exc))
                    finally:
                        failure_log.close()
                raise
        return wrapped
    return decorate


def _merge_tracked_universe(tracked_rows: list[dict], new_candidates: list[Candidate],
                            as_of: str, max_age_days: int,
                            protected_symbols: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Update the rolling research universe and expire stale non-positions."""
    cutoff = date.fromisoformat(as_of) - timedelta(days=max_age_days)
    protected = {symbol.upper() for symbol in (protected_symbols or set())}
    tracked: dict[str, dict] = {}
    for source in tracked_rows:
        symbol = source.get("symbol", "").upper()
        if not symbol or source.get("active", "true").lower() != "true":
            continue
        try:
            last_seen = date.fromisoformat(source.get("last_seen") or source.get("added_on"))
        except (TypeError, ValueError):
            continue
        if last_seen >= cutoff or symbol in protected:
            tracked[symbol] = dict(source)
    by_new = {c.symbol: c for c in new_candidates}
    for candidate in new_candidates:
        previous = tracked.get(candidate.symbol, {})
        row = {
            **previous,
            "symbol": candidate.symbol,
            "added_on": previous.get("added_on", as_of),
            "last_seen": as_of,
            "active": "true",
            "screener_reason": candidate.screener_reason,
            **candidate.screener_values,
        }
        tracked[candidate.symbol] = row
    candidate_rows = []
    for symbol in sorted(tracked):
        source = tracked[symbol]
        current = by_new.get(symbol)
        candidate_rows.append({
            "symbol": symbol,
            "as_of": as_of,
            "screener_reason": current.screener_reason if current else "existing tracked universe",
            **{k: v for k, v in source.items() if k.startswith("screen_")},
        })
    return list(tracked.values()), candidate_rows


@_new_runs_fail_closed("scan")
def scan(args) -> None:
    config = load_config(args.config)
    candidates = read_candidates(args.candidates)
    if not candidates:
        raise RuntimeError("candidate file is empty")
    shorts = read_short(args.short) if args.short and Path(args.short).exists() else {}
    fundamentals = (read_fundamentals(args.fundamentals)
                    if args.fundamentals and Path(args.fundamentals).exists() else {})
    option_history = (read_option_history(args.option_history)
                      if args.option_history and Path(args.option_history).exists() else {})
    option_regime_path = getattr(args, "option_regime", None)
    option_regimes = (read_option_regimes(option_regime_path)
                      if option_regime_path and Path(option_regime_path).exists() else {})
    sizing_config = read_json(args.sizing) if args.sizing else {}
    broker_equity = None
    account_state = getattr(args, "account_state", None)
    if account_state:
        account = read_json(account_state)
        broker_equity = float(account["strategy_equity"])
        if broker_equity <= 0 or account.get("base_currency") != "USD":
            raise ValueError("account state must contain positive USD strategy_equity")
        observed = datetime.fromisoformat(str(account["observed_at"]).replace("Z", "+00:00"))
        scan_as_of = date.fromisoformat(args.as_of or str(candidates[0].as_of))
        if not account_snapshot_matches_session(observed, scan_as_of):
            raise ValueError(
                "account state must be from the scan session or its next-session pre-open window")
    positions_path = getattr(args, "positions", None)
    if config.require_position_sizing and not positions_path:
        raise ValueError("--positions is required when position sizing/portfolio gates are enabled")
    if config.require_position_sizing and (not sizing_config or broker_equity is None):
        raise ValueError("AUTO sizing requires --sizing and a current --account-state")
    portfolio_positions = (read_positions(positions_path)
                           if positions_path and Path(positions_path).exists() else [])
    log = TradingLog(args.log_db) if args.log_db else None
    run_as_of = args.as_of or (str(candidates[0].as_of) if candidates else "")
    run_id = (log.new_run(run_as_of, run_type="scan", data_source="local_snapshots",
                          config_sha256=config_sha256(config),
                          config=config_payload(config), app_version=__version__) if log else "")
    rows = []
    new_iv_rows: list[dict] = []
    for c in candidates:
        if args.as_of:
            c = replace(c, as_of=date.fromisoformat(args.as_of))
        bars = read_bars(Path(args.bars_dir) / f"{c.symbol}.csv")
        profile_path = Path(args.intraday_dir) / f"{c.symbol}.csv" if args.intraday_dir else None
        profile_bars = read_bars(profile_path) if profile_path and profile_path.exists() else None
        option_path = Path(args.options_dir) / f"{c.symbol}.csv" if args.options_dir else None
        options = read_options(option_path) if option_path and option_path.exists() else []
        from .metrics import panic_metrics
        panic = panic_metrics(
            bars, config.min_volume_zscore, config.min_range_atr,
            config.max_close_location,
        )
        shock_day = (date.fromisoformat(str(panic["recent_shock_day"]))
                     if panic.get("recent_shock_day") else None)
        shock_options = None
        shock_option_day = None
        shock_snapshot_error = ""
        if shock_day is not None and shock_day != c.as_of:
            snapshot_root_arg = getattr(args, "option_snapshot_root", None)
            snapshot_root = (Path(snapshot_root_arg) if snapshot_root_arg else
                             Path(args.options_dir).parent if args.options_dir else Path("data"))
            try:
                shock_options, shock_option_day = load_option_snapshot(
                    snapshot_root, c.symbol, shock_day,
                )
            except Exception as exc:
                # A corrupt/mislabeled archive is not allowed to abort the
                # universe. evaluate() receives no historical chain and makes
                # the option-panic leg UNKNOWN/fail-closed for this symbol.
                shock_snapshot_error = f"{type(exc).__name__}: {exc}"
        # First pass chooses the contract; second pass applies the resolved
        # fixed-risk or validated-Kelly sizing to the conservative ask.
        # One valid value per prior trading date. Duplicate or non-finite rows
        # cannot accelerate the local-IV qualification clock.
        iv_by_day = {
            x.day: float(x.near_atm_iv)
            for x in option_history.get(c.symbol, [])
            if (x.day < c.as_of and x.near_atm_iv is not None
                and math.isfinite(float(x.near_atm_iv)) and x.near_atm_iv > 0)
        }
        history = [iv_by_day[day] for day in sorted(iv_by_day)]
        previous_iv_days = [day for day in sorted(iv_by_day)
                            if day < (shock_day or c.as_of)]
        previous_iv = iv_by_day[previous_iv_days[-1]] if previous_iv_days else None
        e = evaluate(c, bars, options, shorts.get(c.symbol), fundamentals.get(c.symbol), config,
                     sizing=None, sizing_mode="UNAVAILABLE",
                     previous_near_atm_iv=previous_iv, profile_bars=profile_bars,
                     iv_history=history, option_regime=option_regimes.get(c.symbol),
                     portfolio_positions=portfolio_positions,
                     shock_options=shock_options, shock_option_day=shock_option_day)
        selected = e.selected_option
        resolution = resolve_sizing(
            sizing_config,
            equity=broker_equity,
            entry_price=selected.ask if selected else None,
            target_price=e.metrics.get("scenario_target_option_price"),
            stop_price=e.metrics.get("scenario_stop_option_price"),
            multiplier=selected.multiplier if selected else 100,
            strategy_version=config.strategy_version,
            local_iv_observations=len(history),
            min_local_iv_observations=config.min_iv_history_observations,
            as_of=c.as_of,
            evidence_base_dir=Path(args.sizing).parent,
        )
        limits = (portfolio_limits(sizing_config, resolution.mode)
                  if resolution.sizing is not None else None)
        if resolution.sizing:
            e = evaluate(c, bars, options, shorts.get(c.symbol), fundamentals.get(c.symbol), config,
                         sizing=resolution.sizing, sizing_mode=resolution.mode,
                         previous_near_atm_iv=previous_iv, profile_bars=profile_bars,
                         iv_history=history, option_regime=option_regimes.get(c.symbol),
                         portfolio_positions=portfolio_positions, portfolio_limits=limits,
                         shock_options=shock_options, shock_option_day=shock_option_day)
        e.metrics.update({
            "shock_option_snapshot_load_error": shock_snapshot_error,
            "sizing_mode": resolution.mode,
            "validated_sizing_ready": resolution.validated_ready,
            "sizing_transition_blockers": " | ".join(resolution.blockers),
            "sizing_evidence_strategy_version": resolution.evidence.get("strategy_version"),
            "sizing_evidence_sample_end": resolution.evidence.get("sample_end"),
            "sizing_evidence_observation_days": resolution.evidence.get("observation_days"),
            "sizing_evidence_completed_trades": resolution.evidence.get("completed_trades"),
            "sizing_evidence_study_sha256": resolution.evidence.get("study_sha256"),
            # Freeze the capacity policy and equity beside the signal.  The
            # next-session executor re-applies these limits to live prices and
            # to active entry orders, rather than treating each CSV row as an
            # independent order.
            "portfolio_sizing_equity": broker_equity,
            "portfolio_limit_premium_fraction": limits[0] if limits else None,
            "portfolio_limit_risk_fraction": limits[1] if limits else None,
            "portfolio_limit_open_positions": limits[2] if limits else None,
            "portfolio_limit_positions_per_underlying": limits[3] if limits else None,
            "minimum_target_payoff_to_loss": config.option_mc_min_target_payoff_to_loss,
        })
        # Reserve a candidate immediately for the remainder of this scan.  A
        # batch is sequential capital allocation, not N independent snapshots;
        # otherwise two cold-start candidates can both observe an empty book.
        reservation_applied = False
        if (e.status in {"BUY_CANDIDATE", "PILOT_CANDIDATE"} and
                e.selected_option is not None and limits is not None):
            contracts = int(float(e.metrics.get("position_contracts") or 0))
            planned_risk = float(e.metrics.get("position_risk_per_contract") or 0)
            tail_risk = float(
                e.metrics.get("position_capital_at_risk_per_contract") or 0)
            if contracts > 0 and planned_risk > 0 and tail_risk > 0:
                portfolio_positions.append(Position(
                    symbol=e.symbol,
                    option_symbol=e.selected_option.symbol,
                    entry_date=e.as_of,
                    entry_price=e.selected_option.ask,
                    entry_spot=float(e.metrics.get("current_price") or 0),
                    contracts=contracts,
                    risk_per_contract=planned_risk,
                    expiry=e.selected_option.expiry,
                    capital_at_risk_per_contract=tail_risk,
                ))
                reservation_applied = True
        e.metrics["portfolio_same_scan_reservation_applied"] = reservation_applied
        rows.append({"run_id": run_id, "symbol": e.symbol, "as_of": str(e.as_of), "status": e.status,
                     "passed_gates": e.passed_gates, "selected_option": e.selected_option.symbol if e.selected_option else "",
                     "reasons": " | ".join(e.reasons), **e.metrics})
        if e.metrics.get("near_atm_iv") not in (None, ""):
            new_iv_rows.append({"symbol": e.symbol, "day": str(e.as_of), "near_atm_iv": e.metrics["near_atm_iv"]})
        if log:
            log.record_candidate(run_id, c)
            log.record_evaluation(run_id, e)
            previous_mode = log.latest_sizing_mode(c.symbol)
            log.record_sizing_mode(run_id, c.symbol, c.as_of.isoformat(),
                                   resolution.mode, resolution.validated_ready,
                                   list(resolution.blockers), resolution.evidence)
            if previous_mode and previous_mode != resolution.mode:
                log.event(
                    "INFO", "SIZING_MODE_TRANSITION",
                    f"{c.symbol} sizing changed from {previous_mode} to {resolution.mode}",
                    run_id,
                    {"symbol": c.symbol, "from": previous_mode,
                     "to": resolution.mode, "blockers": list(resolution.blockers)},
                )
    if log:
        log.finish_run(run_id); log.close()
    write_rows(args.output, rows)
    if args.option_history and new_iv_rows:
        existing_rows = []
        history_path = Path(args.option_history)
        if history_path.exists():
            with open(history_path, newline="") as f:
                existing_rows = list(csv.DictReader(f))
        by_key = {(r["symbol"].upper(), r["day"]): r for r in existing_rows}
        by_key.update({(r["symbol"], r["day"]): r for r in new_iv_rows})
        write_rows(history_path, sorted(by_key.values(), key=lambda r: (r["symbol"], r["day"])))
    print(json.dumps(rows, indent=2, default=str))


@_new_runs_fail_closed("exit")
def exits(args) -> None:
    config = load_config(args.config)
    positions = [p for p in read_positions(args.positions) if p.status == "OPEN" and p.contracts > 0]
    with open(args.signals, newline="") as f:
        evaluations = {r["symbol"]: r for r in csv.DictReader(f)}
    log = TradingLog(args.log_db) if args.log_db else None
    as_of = args.as_of or (max((p.entry_date for p in positions), default=date.today()).isoformat())
    run_id = (log.new_run(as_of, run_type="exit", data_source="local_snapshots",
                          config_sha256=config_sha256(config), config=config_payload(config),
                          app_version=__version__) if log else "")
    rows = []
    for p in positions:
        if log:
            prior_marks = []
            for (payload,) in log.db.execute("SELECT decision_json FROM exit_decisions WHERE option_symbol=?", (p.option_symbol,)):
                try:
                    mark = json.loads(payload).get("current_option_bid")
                    if mark not in (None, ""):
                        prior_marks.append(float(mark))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
            if prior_marks:
                p = replace(p, max_option_price=max([p.max_option_price or p.entry_price, *prior_marks]))
        bars = read_bars(Path(args.bars_dir) / f"{p.symbol}.csv")
        raw = evaluations.get(p.symbol, {})
        def truth(name: str, default: bool = False) -> bool:
            return str(raw.get(name, str(default))).lower() in {"true", "1", "yes"}
        def number(name: str):
            try:
                return float(raw[name]) if raw.get(name, "") != "" else None
            except (ValueError, TypeError):
                return None
        e = Evaluation(p.symbol, bars[-1].day if bars else p.entry_date, raw.get("status", "NO_TRADE"),
                       {"thesis_not_broken": not truth("valuation_regime_break") and truth("thesis_not_broken", True)},
                       {"valuation_regime_break": truth("valuation_regime_break"), "worsening": truth("worsening"),
                        "spread_pct": number("spread_pct"), "iv": number("iv"),
                        "exit_context_present": bool(raw)})
        current_option = None
        if args.options_dir:
            option_path = Path(args.options_dir) / f"{p.symbol}.csv"
            if option_path.exists():
                current_option = next((o for o in read_options(option_path) if o.symbol == p.option_symbol), None)
        decision = evaluate_exit(p, bars, e, current_option=current_option, config=config,
                                 as_of=date.fromisoformat(args.as_of) if args.as_of else None)
        rows.append(decision)
        if log:
            log.record_exit_decision(run_id, str(bars[-1].day if bars else as_of), decision)
    if log:
        log.finish_run(run_id); log.close()
    write_rows(args.output, rows, fieldnames=(None if rows else [
        "symbol", "option_symbol", "action", "reason", "triggers", "missing_inputs"
    ]))
    print(json.dumps(rows, indent=2, default=str))


def screener(args) -> None:
    """Run Moomoo Stock Screening V2 as the preferred coarse-universe source."""
    from .moomoo_adapter import MoomooProvider
    config = load_config(args.config)
    provider = MoomooProvider(args.host, args.port)
    try:
        raw = provider.screen_us_panic_candidates(config)
    finally:
        provider.close()
    rows = [{"symbol": x["symbol"], "as_of": args.as_of,
             "screener_reason": "Moomoo Stock Screening V2 coarse panic screen",
             "screen_current_price": x["price"], "screen_market_cap": x["market_cap"],
             "screen_volume_ratio": x["volume_ratio"], "screen_change_5d": x["change_5d"],
             "screen_avg_volume_20d": x["avg_volume_20d"],
             "screen_avg_dollar_volume_20d": x["avg_dollar_volume_20d"]}
            for x in raw]
    columns = ["symbol", "as_of", "screener_reason", "screen_current_price", "screen_market_cap",
               "screen_volume_ratio", "screen_change_5d", "screen_avg_volume_20d",
              "screen_avg_dollar_volume_20d"]
    write_rows(args.output, rows, fieldnames=columns)
    if args.log_db:
        log = TradingLog(args.log_db)
        run_id = log.new_run(args.as_of, run_type="screener", data_source="moomoo_stock_screen_v2",
                             metadata={"candidate_count": len(rows)},
                             config_sha256=config_sha256(config), config=config_payload(config),
                             app_version=__version__)
        for row in rows:
            log.record_candidate(run_id, Candidate(
                row["symbol"], date.fromisoformat(args.as_of), row["screener_reason"],
                {k: float(v) for k, v in row.items() if k.startswith("screen_") and v != ""}))
        log.finish_run(run_id); log.close()
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def research(args) -> dict:
    from .fundamental_research import FundamentalResearchClient
    client = FundamentalResearchClient(model=args.model)
    result = client.review(args.symbol, Path(args.packet).read_text(encoding="utf-8"),
                           not args.no_web_search, args.as_of)
    write_json(args.output, result)
    if args.fundamentals_out:
        merge_fundamental(args.fundamentals_out, args.symbol, result, args.as_of)
    if args.log_db:
        log = TradingLog(args.log_db)
        run_id = log.new_run(args.as_of, run_type="fundamental_research",
                             data_source=result.get("retrieval_mode", "NO_VERIFIED_RETRIEVAL"),
                             model=client.model, metadata={"symbol": args.symbol, "result": result},
                             app_version=__version__)
        log.finish_run(run_id); log.close()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def research_capabilities(args) -> None:
    """Run a deliberately explicit, billable model capability probe."""
    if not getattr(args, "billable_probe", False):
        raise ValueError("research-capabilities requires --billable-probe")
    from .fundamental_research import FundamentalResearchClient
    client = FundamentalResearchClient(model=args.model)
    result = client.probe_native_web_search()
    result.update({
        "probe_run": True,
        "billable_probe": True,
        "point_in_time_evidence": False,
    })
    print(json.dumps(result, indent=2, ensure_ascii=False))


def _configured_news_sources(selection: str | None):
    """Resolve every usable provider while retaining setup failures as diagnostics."""
    from .news import configured_providers, provider_configuration
    configuration = provider_configuration(selection)
    providers = []
    diagnostics = []
    for row in configuration["providers"]:
        if not row["configured"]:
            diagnostics.append({
                "provider": row["provider"], "ok": False,
                "gate_eligible": row["gate_eligible"],
                "broad_news_coverage": row["broad_news_coverage"],
                "error": "provider credentials are not configured",
            })
            continue
        try:
            providers.extend(configured_providers(row["provider"]))
        except Exception as exc:
            diagnostics.append({
                "provider": row["provider"], "ok": False,
                "gate_eligible": row["gate_eligible"],
                "broad_news_coverage": row["broad_news_coverage"],
                "error": f"{type(exc).__name__}: {exc}",
            })
    return providers, diagnostics, configuration


def _news_local_context(directory: Path | None, symbol: str) -> tuple[str, str]:
    if directory is None:
        return "", ""
    for suffix in (".json", ".txt"):
        path = directory / f"{symbol.upper()}{suffix}"
        if path.exists():
            return path.read_text(encoding="utf-8"), str(path)
    return "", ""


def _sync_news_symbol(symbol: str, start: datetime, end: datetime, state_dir: Path,
                      providers, setup_diagnostics: list[dict],
                      local_packet_dir: Path | None = None) -> tuple[dict, Path]:
    from .news import (append_news_archive, collect_news, evidence_packet,
                       read_news_archive)
    symbol = symbol.upper()
    archive_path = state_dir / "news" / "archive" / f"{symbol}.jsonl"
    existing = read_news_archive(archive_path)
    observed, diagnostics = collect_news(symbol, start, end, providers, existing)
    appended = append_news_archive(archive_path, observed)
    archived = read_news_archive(archive_path)
    local_context, local_path = _news_local_context(local_packet_dir, symbol)
    packet = evidence_packet(
        symbol, start, end, archived, [*setup_diagnostics, *diagnostics], local_context,
    )
    packet.update({
        "archive_path": str(archive_path),
        "archive_row_count": len(archived),
        "archive_appended": appended,
        "local_packet_path": local_path,
    })
    packet_path = state_dir / "news" / "packets" / end.astimezone(
        ZoneInfo("America/New_York")).date().isoformat() / f"{symbol}.json"
    write_json(packet_path, packet)
    return packet, packet_path


def _news_window(start: str, as_of: str) -> tuple[datetime, datetime]:
    from .news import cutoff_end
    ny = ZoneInfo("America/New_York")
    start_at = datetime.combine(date.fromisoformat(start), datetime.min.time(), tzinfo=ny)
    end_at = cutoff_end(as_of)
    if start_at > end_at:
        raise ValueError("news start must be on or before as-of")
    return start_at, end_at


def news_sync(args) -> None:
    """Append provider observations and materialize dated PIT evidence packets."""
    symbols = sorted({value.strip().upper() for value in args.symbols.split(",") if value.strip()})
    if not symbols:
        raise ValueError("at least one symbol is required")
    start, end = _news_window(args.start, args.as_of)
    providers, setup_diagnostics, configuration = _configured_news_sources(args.providers)
    state_dir = Path(args.state_dir)
    local_dir = Path(args.local_packet_dir) if args.local_packet_dir else None
    results = []
    for symbol in symbols:
        try:
            packet, path = _sync_news_symbol(
                symbol, start, end, state_dir, providers, setup_diagnostics, local_dir,
            )
            results.append({
                "symbol": symbol, "ok": True, "packet": str(path),
                "article_count": packet["article_count"],
                "packet_sha256": packet["packet_sha256"],
                "archive_appended": packet["archive_appended"],
            })
        except Exception as exc:
            results.append({
                "symbol": symbol, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    print(json.dumps({
        "ok": all(row["ok"] for row in results),
        "provider_configuration": configuration,
        "results": results,
    }, indent=2, ensure_ascii=False))


def _fundamental_fail_closed(symbol: str, as_of: str, packet: dict,
                             reason: str) -> dict:
    return {
        "symbol": symbol.upper(),
        "thesis_status": "uncertain",
        "event_class": "UNKNOWN",
        "selloff_explanation": reason,
        "material_news_found": bool(packet.get("articles")),
        "catalyst_present": False,
        "catalyst": "",
        "valuation_regime_break": False,
        "valuation_reason": "Insufficient verified point-in-time broad-news coverage.",
        "as_of": as_of,
        "evidence": [],
        "sources": [],
        "source_ids": [],
        "confidence": 0.0,
        "needs_review": True,
        "news_packet_sha256": packet.get("packet_sha256", ""),
        "point_in_time_collection_ok": False,
        "retrieval_mode": ("POINT_IN_TIME_PACKET_INSUFFICIENT_COVERAGE"
                           if packet.get("articles") else "NO_VERIFIED_RETRIEVAL"),
        "native_web_search_requested": False,
        "native_web_search_observed": False,
    }


def _apply_trade_payload(payload: dict, log_db: str, positions_out: str | None) -> str:
    """Validate one actual fill, journal it, and atomically materialize the position view."""
    from .models import Position
    action = str(payload["action"]).upper()
    if action not in {"OPEN", "REDUCE", "CLOSE", "EXIT"}:
        raise ValueError("action must be OPEN, REDUCE, CLOSE, or EXIT")
    quantity = int(payload.get("quantity", payload.get("contracts", 1)))
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if float(payload["spot_price"]) <= 0 or float(payload["option_price"]) <= 0:
        raise ValueError("actual spot_price and option_price must be positive")
    fees = float(payload.get("fees", 0))
    if not math.isfinite(fees) or fees < 0:
        raise ValueError("fees must be a finite non-negative amount")
    existing_positions = read_positions(positions_out) if positions_out and Path(positions_out).exists() else []
    updated_positions = list(existing_positions)
    if action == "OPEN":
        actual_entry_price = float(payload["option_price"])
        actual_entry_spot = float(payload["spot_price"])
        supplied_entry_price = float(payload.get("entry_price", actual_entry_price))
        supplied_entry_spot = float(payload.get("entry_spot", actual_entry_spot))
        if abs(supplied_entry_price - actual_entry_price) > 0.0001:
            raise ValueError("OPEN entry_price must equal the actual option_price fill")
        if abs(supplied_entry_spot - actual_entry_spot) > 0.0001:
            raise ValueError("OPEN entry_spot must equal the actual spot_price snapshot")
        multiplier = int(payload.get("multiplier", 100))
        minimum_capital_risk = (
            actual_entry_price * multiplier + fees / quantity
        )
        capital_at_risk = float(payload.get(
            "capital_at_risk_per_contract", minimum_capital_risk))
        raw_partial = payload.get("partial_exit_taken", False)
        partial_exit_taken = (
            str(raw_partial).lower() in {"true", "1", "yes"}
            if isinstance(raw_partial, str) else bool(raw_partial)
        )
        position = Position(
            symbol=payload["symbol"].upper(),
            option_symbol=payload["option_symbol"],
            entry_date=date.fromisoformat(payload["entry_date"]),
            entry_price=actual_entry_price,
            entry_spot=actual_entry_spot,
            contracts=int(payload.get("contracts", quantity)),
            risk_per_contract=float(payload["risk_per_contract"]),
            expiry=date.fromisoformat(payload["expiry"]),
            thesis=payload.get("thesis", ""),
            current_option_price=payload.get("current_option_price",
                                             actual_entry_price),
            entry_atr=payload.get("entry_atr"),
            hard_stop_spot=payload.get("hard_stop_spot"),
            option_stop_price=payload.get("option_stop_price"),
            target_spot=payload.get("target_spot"),
            target_option_price=payload.get("target_option_price"),
            target_source=payload.get("target_source", ""),
            entry_iv=payload.get("entry_iv"),
            entry_iv_percentile=payload.get("entry_iv_percentile"),
            max_hold_days=int(payload.get("max_hold_days", 30)),
            max_option_price=payload.get("max_option_price",
                                         actual_entry_price),
            status=payload.get("status", "OPEN"),
            multiplier=multiplier,
            currency=str(payload.get("currency", "USD")).upper(),
            capital_at_risk_per_contract=capital_at_risk,
            partial_exit_taken=partial_exit_taken,
        )
        if position.contracts != quantity:
            raise ValueError("OPEN quantity must equal contracts")
        if position.entry_date != date.fromisoformat(payload["trade_date"]):
            raise ValueError("OPEN entry_date must equal trade_date")
        if position.entry_date >= position.expiry:
            raise ValueError("OPEN trade date must be before expiry")
        if position.status != "OPEN":
            raise ValueError("OPEN must create an OPEN position")
        if position.multiplier <= 0 or position.risk_per_contract <= 0:
            raise ValueError("multiplier and risk_per_contract must be positive")
        if (position.capital_at_risk_per_contract is None or
                not math.isfinite(position.capital_at_risk_per_contract) or
                position.capital_at_risk_per_contract + 1e-9 <
                minimum_capital_risk):
            raise ValueError(
                "capital_at_risk_per_contract cannot understate paid premium plus entry fees")
        if position.partial_exit_taken:
            raise ValueError("OPEN cannot start with partial_exit_taken")
        if position.currency != "USD":
            raise ValueError("LongVol US option positions must be denominated in USD")
        if position.entry_atr is None or position.entry_atr <= 0:
            raise ValueError("OPEN requires a positive frozen entry_atr")
        if position.hard_stop_spot is None or not 0 < position.hard_stop_spot < position.entry_spot:
            raise ValueError("OPEN requires hard_stop_spot below entry_spot")
        if position.option_stop_price is None or not 0 <= position.option_stop_price < position.entry_price:
            raise ValueError("OPEN requires option_stop_price below entry_price")
        minimum_risk = (position.entry_price - position.option_stop_price) * position.multiplier
        if position.risk_per_contract + 1e-9 < minimum_risk:
            raise ValueError("risk_per_contract cannot understate the frozen option-stop loss")
        if position.target_spot is None or position.target_spot <= position.entry_spot:
            raise ValueError("OPEN requires target_spot above entry_spot")
        if position.target_option_price is None or position.target_option_price <= position.entry_price:
            raise ValueError("OPEN requires target_option_price above entry_price")
        if not position.target_source:
            raise ValueError("OPEN requires target_source")
        if position.entry_iv is None or position.entry_iv <= 0:
            raise ValueError("OPEN requires a positive entry_iv")
        if (position.entry_iv_percentile is None or
                not 0 <= position.entry_iv_percentile <= 1):
            raise ValueError("OPEN requires entry_iv_percentile in [0, 1]")
        if position.max_hold_days <= 0:
            raise ValueError("OPEN requires a positive max_hold_days")
        duplicate = next((p for p in existing_positions if p.option_symbol == position.option_symbol and
                          p.status == "OPEN" and p.contracts > 0), None)
        if duplicate:
            raise ValueError("an OPEN position already exists for this option_symbol")
        updated_positions.append(position)
    else:
        if not positions_out:
            raise ValueError("--positions-out is required for REDUCE/CLOSE/EXIT")
        option_symbol = payload["option_symbol"]
        position = next((p for p in existing_positions if p.option_symbol == option_symbol and
                         p.status == "OPEN" and p.contracts > 0), None)
        if position is None:
            raise ValueError("no matching OPEN position")
        if quantity > position.contracts:
            raise ValueError("exit quantity exceeds open contracts")
        if action == "REDUCE" and quantity >= position.contracts:
            raise ValueError("REDUCE must leave at least one open contract; use CLOSE or EXIT")
        if action == "REDUCE" and position.partial_exit_taken:
            raise ValueError("a position can take only one partial target exit")
        if action in {"CLOSE", "EXIT"} and quantity != position.contracts:
            raise ValueError("CLOSE/EXIT quantity must equal all open contracts")
        remaining = position.contracts - quantity
        replacement = replace(
            position, contracts=remaining,
            status="OPEN" if remaining else "CLOSED",
            current_option_price=float(payload["option_price"]),
            max_option_price=max(position.max_option_price or
                                 position.entry_price,
                                 float(payload["option_price"])),
            partial_exit_taken=(position.partial_exit_taken or
                                action == "REDUCE"),
        )
        updated_positions = [replacement if p.option_symbol == option_symbol and p.status == "OPEN" else p
                             for p in existing_positions]
        position = replacement
    log = TradingLog(log_db)
    try:
        trade_id = log.record_trade(action, position, payload["trade_date"], quantity,
                                    float(payload["spot_price"]), float(payload["option_price"]), float(payload.get("fees", 0)),
                                    payload.get("run_id"), payload.get("features", {}), payload.get("notes", ""),
                                    payload.get("broker_order_id", ""), payload.get("currency", "USD"))
    finally:
        log.close()
    if positions_out:
        rows = []
        for item in updated_positions:
            row = asdict(item)
            row["entry_date"] = item.entry_date.isoformat(); row["expiry"] = item.expiry.isoformat()
            rows.append(row)
        write_rows(positions_out, rows)
    return trade_id


def _record_trade_unlocked(args) -> None:
    trade_id = _apply_trade_payload(read_json(args.trade_json), args.log_db,
                                    args.positions_out)
    print(json.dumps({"trade_id": trade_id}))


def record_trade(args) -> None:
    """Record a fill under an inter-process lock before materializing positions."""
    from .ops import exclusive_lock
    anchor = Path(args.positions_out or args.log_db)
    lock_path = anchor.with_suffix(anchor.suffix + ".lock")
    with exclusive_lock(lock_path):
        _record_trade_unlocked(args)


def _csv_safe_rows(rows: list[dict]) -> list[dict]:
    return [{key: (json.dumps(value, default=str, sort_keys=True)
                   if isinstance(value, (dict, list, tuple, set)) else value)
             for key, value in row.items()} for row in rows]


def _terminal_broker_fill(row: dict) -> bool:
    dealt = float(row.get("dealt_qty") or 0)
    ordered = float(row.get("quantity") or 0)
    status = str(row.get("status") or "").upper()
    if dealt <= 0:
        return False
    if ordered > 0 and dealt >= ordered - 1e-9:
        return True
    return any(token in status for token in
               ("CANCEL", "FAILED", "DISABLED", "DELETED"))


def _active_broker_order(row: dict) -> bool:
    status = str(row.get("order_status") or row.get("orderStatus") or "").upper()
    dealt = float(row.get("dealt_qty") or row.get("fillQty") or 0)
    ordered = float(row.get("qty") or row.get("quantity") or 0)
    if ordered > 0 and dealt >= ordered - 1e-9:
        return False
    return not any(token in status for token in
                   ("FILLED_ALL", "CANCEL", "FAILED", "DISABLED", "DELETED"))


def _broker_trade_day(order: dict, fallback: date) -> str:
    for key in ("updated_time", "updatedTime", "create_time", "createTime"):
        raw = str(order.get(key) or "")
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except ValueError:
            continue
    return fallback.isoformat()


def _materialize_broker_fills(log: TradingLog, run_id: str, environment: str,
                              account_id: int, positions_out: str,
                              estimated_fee_per_contract: float) -> list[dict]:
    """Turn terminal, known LongVol broker fills into the audited position ledger."""
    from .ops import exclusive_lock

    results: list[dict] = []
    lock_path = Path(positions_out).with_suffix(Path(positions_out).suffix + ".lock")
    with exclusive_lock(lock_path):
        for row in log.pending_fill_rows(environment, account_id):
            if not _terminal_broker_fill(row):
                continue
            broker_order_id = str(row.get("broker_order_id") or "")
            existing_trade = log.trade_for_broker_order(broker_order_id)
            if existing_trade:
                log.mark_fill_materialized(int(row["id"]), float(row["dealt_qty"]),
                                           existing_trade)
                results.append({"broker_order_id": broker_order_id,
                                "trade_id": existing_trade, "already_recorded": True})
                continue
            try:
                dealt = float(row["dealt_qty"])
                quantity = int(round(dealt))
                if quantity <= 0 or abs(dealt - quantity) > 1e-9:
                    raise ValueError("option fill quantity must be a positive whole number")
                fill = float(row["dealt_avg_price"])
                if fill <= 0:
                    raise ValueError("terminal fill has no positive dealt_avg_price")
                intent = json.loads(row["intent_json"])
                metadata = intent.get("metadata") or {}
                order = json.loads(row["order_json"])
                trade_day = _broker_trade_day(order, datetime.now(
                    ZoneInfo("America/New_York")).date())
                spot = float(metadata.get("submission_spot") or 0)
                if spot <= 0:
                    raise ValueError("known fill has no submission spot snapshot")
                if intent["purpose"] == "ENTRY":
                    signal = metadata.get("signal") or {}
                    stop_price = float(intent.get("option_stop_price") or 0)
                    multiplier = int(intent.get("multiplier") or 100)
                    target_option = float(signal.get("scenario_target_option_price") or 0)
                    target_spot = float(signal.get("target_spot") or 0)
                    entry_iv = float(metadata.get("submission_option_iv") or
                                     signal.get("selected_option_iv") or 0)
                    raw_iv_percentile = signal.get("active_iv_percentile")
                    iv_percentile = float(raw_iv_percentile) if raw_iv_percentile not in (None, "") else -1
                    if not 0 <= stop_price < fill < target_option:
                        raise ValueError("actual entry fill is outside the frozen option scenario")
                    payload = {
                        "action": "OPEN", "symbol": str(signal.get("symbol") or ""),
                        "option_symbol": intent["code"], "trade_date": trade_day,
                        "entry_date": trade_day, "option_price": fill,
                        "entry_price": fill, "spot_price": spot, "entry_spot": spot,
                        "quantity": quantity, "contracts": quantity,
                        "risk_per_contract": ((fill - stop_price) * multiplier +
                                              2 * estimated_fee_per_contract),
                        "capital_at_risk_per_contract": (
                            fill * multiplier + estimated_fee_per_contract),
                        "partial_exit_taken": False,
                        "expiry": signal.get("selected_option_expiry"),
                        "thesis": str(signal.get("reasons") or "LongVol gated signal"),
                        "entry_atr": signal.get("atr"),
                        "hard_stop_spot": intent.get("hard_stop_spot"),
                        "option_stop_price": stop_price,
                        "target_spot": target_spot,
                        "target_option_price": target_option,
                        "target_source": signal.get("target_source"),
                        "entry_iv": entry_iv,
                        "entry_iv_percentile": iv_percentile,
                        "max_hold_days": int(float(signal.get("max_holding_days") or 30)),
                        "current_option_price": fill, "max_option_price": fill,
                        "multiplier": multiplier, "currency": "USD",
                        "fees": estimated_fee_per_contract * quantity,
                        "run_id": run_id, "features": signal,
                        "broker_order_id": broker_order_id,
                        "notes": "Auto-materialized Moomoo terminal fill; fees, spot and IV are execution-time estimates/snapshots.",
                    }
                else:
                    open_positions = [position for position in read_positions(positions_out)
                                      if position.status == "OPEN" and position.contracts > 0]
                    position = next((item for item in open_positions
                                     if item.option_symbol == intent["code"]), None)
                    if position is None:
                        raise ValueError("exit fill has no matching local OPEN position")
                    if quantity > position.contracts:
                        raise ValueError("exit fill exceeds the local OPEN position")
                    action = ("REDUCE" if quantity < position.contracts else
                              ("EXIT" if intent["purpose"] == "RISK_EXIT" else "CLOSE"))
                    payload = {
                        "action": action, "symbol": position.symbol,
                        "option_symbol": position.option_symbol,
                        "trade_date": trade_day, "option_price": fill,
                        "spot_price": spot, "quantity": quantity,
                        "currency": "USD", "fees": estimated_fee_per_contract * quantity,
                        "run_id": run_id,
                        "features": {"intraday_reason": metadata.get("reason", "")},
                        "broker_order_id": broker_order_id,
                        "notes": "Auto-materialized Moomoo terminal exit fill; fees and spot are execution-time estimates/snapshots.",
                    }
                trade_id = _apply_trade_payload(payload, log.db.execute(
                    "PRAGMA database_list").fetchone()[2], positions_out)
                log.mark_fill_materialized(int(row["id"]), dealt, trade_id)
                results.append({"broker_order_id": broker_order_id,
                                "trade_id": trade_id, "quantity": quantity})
            except Exception as exc:
                log.event("ERROR", "FILL_MATERIALIZATION_FAILED", str(exc), run_id,
                          {"broker_order_id": broker_order_id,
                           "client_order_id": row.get("client_order_id")})
                results.append({"broker_order_id": broker_order_id,
                                "error": str(exc)})
    return results


def account_sync(args) -> None:
    """Synchronize one explicitly resolved US trading account."""
    from .broker import MoomooBroker, load_broker_config

    config = load_broker_config(args.trading_config)
    broker = MoomooBroker(config, args.host, args.port)
    log = TradingLog(args.log_db) if args.log_db else None
    run_id = ""
    try:
        observed_at = datetime.now(ZoneInfo("America/New_York"))
        account = broker.account_snapshot(observed_at)
        positions = broker.positions()
        current_orders = broker.orders()
        historical_orders = broker.history_orders(observed_at.date() - timedelta(days=30),
                                                  observed_at.date())
        by_order_key = {}
        for order in [*historical_orders, *current_orders]:
            key = str(order.get("order_id") or order.get("orderID") or
                      order.get("remark") or json.dumps(order, sort_keys=True, default=str))
            by_order_key[key] = order
        orders = list(by_order_key.values())
        local_positions = (read_positions(args.local_positions)
                           if args.local_positions and Path(args.local_positions).exists() else [])
        mismatches = []
        for local in local_positions:
            if local.status != "OPEN" or local.contracts <= 0:
                continue
            broker_qty = _broker_position_quantity(positions, local.option_symbol)
            if abs(broker_qty - local.contracts) > 1e-9:
                mismatches.append({"option_symbol": local.option_symbol,
                                   "local_contracts": local.contracts,
                                   "broker_contracts": broker_qty})
        if log:
            run_id = log.new_run(observed_at.date().isoformat(), run_type="account_sync",
                                 data_source="moomoo_trade_api",
                                 metadata={"environment": config.environment},
                                 app_version=__version__)
            log.record_account_snapshot(account.as_dict(), run_id)
            reconciled = sum(log.reconcile_broker_order(
                config.environment, account.account_id, order) for order in orders)
            materialized = _materialize_broker_fills(
                log, run_id, config.environment, account.account_id,
                args.local_positions, config.estimated_fees_per_contract,
            ) if args.local_positions else []
            if args.local_positions and Path(args.local_positions).exists():
                local_positions = read_positions(args.local_positions)
                mismatches = []
                local_by_code = {local.option_symbol: local.contracts
                                 for local in local_positions
                                 if local.status == "OPEN" and local.contracts > 0}
                known_codes = {str(item[0]) for item in log.db.execute(
                    "SELECT DISTINCT code FROM broker_orders WHERE environment=? AND account_id=?",
                    (config.environment, str(account.account_id)))}
                for local in local_positions:
                    if local.status != "OPEN" or local.contracts <= 0:
                        continue
                    broker_qty = _broker_position_quantity(positions, local.option_symbol)
                    if abs(broker_qty - local.contracts) > 1e-9:
                        mismatches.append({"option_symbol": local.option_symbol,
                                           "local_contracts": local.contracts,
                                           "broker_contracts": broker_qty})
                for code in sorted(known_codes - set(local_by_code)):
                    broker_qty = _broker_position_quantity(positions, code)
                    if broker_qty > 0:
                        mismatches.append({"option_symbol": code,
                                           "local_contracts": 0,
                                           "broker_contracts": broker_qty})
            log.finish_run(run_id)
        else:
            reconciled = 0
            materialized = []
        write_json(args.output, {**account.as_dict(), "position_mismatches": mismatches})
        if args.positions_out:
            write_rows(args.positions_out, _csv_safe_rows(positions))
        if args.orders_out:
            write_rows(args.orders_out, _csv_safe_rows(orders))
        print(json.dumps({"account": account.as_dict(), "broker_positions": len(positions),
                          "broker_orders": len(orders), "orders_reconciled": reconciled,
                          "fills_materialized": materialized,
                          "position_mismatches": mismatches},
                         indent=2, ensure_ascii=False))
        if mismatches:
            raise RuntimeError("broker/local LongVol position mismatch; new decisions are blocked")
    except Exception as exc:
        if log and run_id:
            log.finish_run(run_id, "FAILED", str(exc))
        raise
    finally:
        if log:
            log.close()
        broker.close()


def _broker_position_quantity(rows: list[dict], code: str, *, sellable: bool = False) -> float:
    values = []
    for row in rows:
        if str(row.get("code") or "") != code:
            continue
        raw = row.get("can_sell_qty") if sellable and "can_sell_qty" in row else row.get("qty")
        values.append(float(raw or 0))
    return sum(values)


def _submit_intent(broker, account, broker_positions: list[dict], intent,
                   option_quote, underlying_quote, trading_config, args,
                   log: TradingLog, run_id: str) -> dict:
    from dataclasses import asdict
    from .broker import validate_live_order

    local_existing = log.broker_order(
        trading_config.environment, account.account_id, intent.client_order_id)
    if local_existing and local_existing.get("status") != "DRY_RUN":
        try:
            recorded_order = json.loads(local_existing.get("order_json") or "{}")
        except json.JSONDecodeError:
            recorded_order = {}
        return {"submitted": False, "duplicate": True, "local_duplicate": True,
                "order": recorded_order, "status": local_existing.get("status")}
    existing = broker.existing_order(intent.client_order_id)
    if existing:
        result = {"submitted": False, "duplicate": True, "order": existing}
        log.record_broker_order(asdict(intent), result, trading_config.environment,
                                account.account_id, run_id)
        return result
    now = datetime.now(ZoneInfo("America/New_York"))
    validate_live_order(intent, account, option_quote, now, trading_config, underlying_quote)
    if intent.side == "SELL" and _broker_position_quantity(
            broker_positions, intent.code, sellable=True) + 1e-9 < intent.quantity:
        raise RuntimeError("broker position is smaller than the requested exit quantity")
    if intent.purpose == "ENTRY" and _broker_position_quantity(broker_positions, intent.code) > 0:
        raise RuntimeError("broker already holds the entry option")
    try:
        result = broker.place_order(
            intent, submit=args.submit,
            live_confirmation=getattr(args, "live_confirmation", ""),
        )
    except Exception as exc:
        if args.submit and "unknown state" in str(exc).lower():
            log.record_broker_order(
                asdict(intent),
                {"order": {"order_status": "UNKNOWN_SUBMISSION",
                           "last_err_msg": str(exc),
                           "remark": intent.client_order_id}},
                trading_config.environment, account.account_id, run_id,
            )
        raise
    log.record_broker_order(asdict(intent), result, trading_config.environment,
                            account.account_id, run_id)
    return result


def place_order(args) -> None:
    """Validate a live quote and submit one idempotent limit order."""
    from .broker import MoomooBroker, load_broker_config, load_order_intent
    from .moomoo_adapter import MoomooProvider

    config = load_broker_config(args.trading_config)
    intent = load_order_intent(args.intent)
    quote_provider = MoomooProvider(args.host, args.port)
    broker = MoomooBroker(config, args.host, args.port)
    log = TradingLog(args.log_db)
    run_id = log.new_run(datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
                         run_type="place_order",
                         data_source="moomoo_trade_api",
                         metadata={"environment": config.environment,
                                   "submit": bool(args.submit)},
                         app_version=__version__)
    try:
        codes = [intent.code] + ([intent.underlying_code] if intent.underlying_code else [])
        quotes = quote_provider.get_live_quotes(codes)
        if intent.code not in quotes:
            raise RuntimeError("Moomoo did not return a live option quote")
        underlying_quote = quotes.get(intent.underlying_code)
        if underlying_quote is None:
            raise RuntimeError("Moomoo did not return a live underlying quote")
        intent = replace(intent, metadata={
            **(intent.metadata or {}),
            "submission_spot": underlying_quote.last,
            "submission_option_iv": quotes[intent.code].iv,
        })
        account = broker.account_snapshot(datetime.now(ZoneInfo("America/New_York")))
        log.record_account_snapshot(account.as_dict(), run_id)
        result = _submit_intent(
            broker, account, broker.positions(), intent, quotes[intent.code],
            underlying_quote, config, args, log, run_id,
        )
        log.finish_run(run_id)
        print(json.dumps({"environment": config.environment,
                          "account_id": account.account_id, **result},
                         indent=2, ensure_ascii=False, default=str))
    except Exception as exc:
        log.finish_run(run_id, "FAILED", str(exc))
        raise
    finally:
        log.close(); broker.close(); quote_provider.close()


def _client_order_id(day: str, purpose: str, code: str) -> str:
    import hashlib
    digest = hashlib.sha256(f"{day}|{purpose}|{code}".encode()).hexdigest()[:12]
    return f"LV-{day.replace('-', '')}-{purpose}-{digest}"


def _finite_number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _entry_risk_reservation(signal: dict, entry_price: float, quantity: int,
                            fee_per_contract: float,
                            *, client_order_id: str = "",
                            planned_exit_bid: float | None = None) -> dict:
    """Describe both planned-stop and full-premium risk for one entry."""
    multiplier = int(float(signal.get("selected_option_multiplier") or 100))
    planned_exit = planned_exit_bid
    if planned_exit is None:
        planned_exit = _finite_number(
            signal.get("option_mc_planned_option_exit_bid")
        )
    if planned_exit is None:
        planned_exit = _finite_number(signal.get("scenario_stop_option_price"))
    if (quantity <= 0 or multiplier <= 0 or entry_price <= 0 or
            planned_exit is None or not 0 <= planned_exit < entry_price or
            fee_per_contract < 0):
        raise ValueError("entry reservation has invalid price, quantity, multiplier, or planned exit")
    return {
        "client_order_id": client_order_id,
        "symbol": str(signal.get("symbol") or "").upper(),
        "quantity": quantity,
        "planned_risk": (
            (entry_price - planned_exit) * multiplier +
            2 * fee_per_contract
        ) * quantity,
        "full_premium_tail_risk": (
            entry_price * multiplier + fee_per_contract
        ) * quantity,
    }


def _check_entry_batch_capacity(
    signal: dict, entry_price: float, quantity: int, equity: float,
    positions: list[Position], reservations: dict[str, dict],
    fee_per_contract: float, client_order_id: str,
    reservation_error: str = "",
) -> tuple[bool, str, dict, dict | None]:
    """Reapply frozen portfolio limits across positions and pending orders."""
    names = (
        "portfolio_limit_premium_fraction",
        "portfolio_limit_risk_fraction",
        "portfolio_limit_open_positions",
        "portfolio_limit_positions_per_underlying",
    )
    values = [_finite_number(signal.get(name)) for name in names]
    if reservation_error:
        return False, "PENDING_ENTRY_RISK_UNKNOWN", {
            "pending_entry_reservation_error": reservation_error,
        }, None
    if equity <= 0 or any(value is None or value <= 0 for value in values):
        return False, "PORTFOLIO_LIMITS_MISSING", {}, None
    premium_limit, risk_limit, open_limit_raw, underlying_limit_raw = values
    assert premium_limit is not None and risk_limit is not None
    assert open_limit_raw is not None and underlying_limit_raw is not None
    if (premium_limit >= 1 or risk_limit >= 1 or
            not open_limit_raw.is_integer() or not underlying_limit_raw.is_integer()):
        return False, "PORTFOLIO_LIMITS_INVALID", {}, None
    open_limit = int(open_limit_raw)
    underlying_limit = int(underlying_limit_raw)
    active = [position for position in positions
              if position.status == "OPEN" and position.contracts > 0]
    committed_planned = sum(
        position.risk_per_contract * position.contracts for position in active
    ) + sum(float(item["planned_risk"]) for item in reservations.values())
    committed_tail = sum(
        (position.capital_at_risk_per_contract
         if position.capital_at_risk_per_contract is not None and
         position.capital_at_risk_per_contract > 0 else
         position.entry_price * position.multiplier + fee_per_contract) *
        position.contracts for position in active
    ) + sum(float(item["full_premium_tail_risk"])
            for item in reservations.values())
    symbol = str(signal.get("symbol") or "").upper()
    same_underlying = sum(position.symbol.upper() == symbol for position in active)
    same_underlying += sum(item.get("symbol") == symbol
                           for item in reservations.values())
    diagnostics = {
        "portfolio_live_equity": equity,
        "portfolio_committed_planned_risk_with_pending": committed_planned,
        "portfolio_committed_full_premium_tail_risk_with_pending": committed_tail,
        "portfolio_open_positions_with_pending": len(active) + len(reservations),
        "portfolio_same_underlying_with_pending": same_underlying,
        "portfolio_limit_premium_fraction": premium_limit,
        "portfolio_limit_risk_fraction": risk_limit,
        "portfolio_limit_open_positions": open_limit,
        "portfolio_limit_positions_per_underlying": underlying_limit,
    }
    # The active order with this deterministic ID is already included in the
    # committed totals.  Let idempotent submission reconciliation handle it;
    # do not count the same order twice.
    if client_order_id in reservations:
        diagnostics["active_entry_already_reserved"] = True
        return True, "ACTIVE_ENTRY_ALREADY_RESERVED", diagnostics, None
    try:
        proposed = _entry_risk_reservation(
            signal, entry_price, quantity, fee_per_contract,
            client_order_id=client_order_id,
        )
    except ValueError as exc:
        return False, "ENTRY_RISK_RESERVATION_INVALID", {
            **diagnostics, "entry_reservation_error": str(exc),
        }, None
    resulting_planned = committed_planned + float(proposed["planned_risk"])
    resulting_tail = committed_tail + float(proposed["full_premium_tail_risk"])
    diagnostics.update({
        "portfolio_proposed_planned_risk_live": proposed["planned_risk"],
        "portfolio_proposed_full_premium_tail_risk_live":
            proposed["full_premium_tail_risk"],
        "portfolio_resulting_risk_fraction_live": resulting_planned / equity,
        "portfolio_resulting_premium_fraction_live": resulting_tail / equity,
    })
    reasons = []
    if len(active) + len(reservations) + 1 > open_limit:
        reasons.append("PORTFOLIO_POSITION_LIMIT")
    if same_underlying + 1 > underlying_limit:
        reasons.append("PORTFOLIO_UNDERLYING_LIMIT")
    if resulting_planned > equity * risk_limit + 1e-9:
        reasons.append("PORTFOLIO_PLANNED_RISK_LIMIT")
    if resulting_tail > equity * premium_limit + 1e-9:
        reasons.append("PORTFOLIO_FULL_PREMIUM_LIMIT")
    if reasons:
        return False, " | ".join(reasons), diagnostics, None
    return True, "PORTFOLIO_CAPACITY_AVAILABLE", diagnostics, proposed


def _intraday_once(args) -> list[dict]:
    from dataclasses import asdict
    from .broker import MoomooBroker, OrderIntent, load_broker_config
    from .intraday import evaluate_intraday_entry, evaluate_intraday_exit
    from .moomoo_adapter import MoomooProvider

    trading_config = load_broker_config(args.trading_config)
    with open(args.signals, newline="") as f:
        signals = list(csv.DictReader(f))
    positions = [position for position in read_positions(args.positions)
                 if position.status == "OPEN" and position.contracts > 0]
    position_symbols = {position.symbol for position in positions}
    codes = []
    for row in signals:
        if row.get("status") in {"BUY_CANDIDATE", "PILOT_CANDIDATE"} and row.get("symbol") not in position_symbols:
            codes.extend([f"US.{row['symbol']}", row.get("selected_option", "")])
    for position in positions:
        codes.extend([f"US.{position.symbol}", position.option_symbol])
    codes = [code for code in dict.fromkeys(codes) if code]
    provider = MoomooProvider(args.host, args.port)
    broker = None
    log = TradingLog(args.log_db)
    now = datetime.now(ZoneInfo("America/New_York"))
    run_id = log.new_run(now.date().isoformat(), run_type="intraday_monitor",
                         data_source="moomoo_market_snapshot",
                         metadata={"environment": trading_config.environment,
                                   "submit": bool(args.submit)},
                         app_version=__version__)
    output = []
    try:
        entry_signal_days: set[date] = set()
        for signal in signals:
            if signal.get("status") not in {"BUY_CANDIDATE", "PILOT_CANDIDATE"}:
                continue
            try:
                entry_signal_days.add(
                    date.fromisoformat(str(signal.get("as_of") or "")[:10]))
            except ValueError:
                continue
        next_entry_session: dict[date, date] = {}
        calendar_error = ""
        if entry_signal_days:
            calendar_start = min(entry_signal_days) + timedelta(days=1)
            calendar_end = max(
                now.date() + timedelta(days=14),
                max(entry_signal_days) + timedelta(days=14),
            )
            try:
                trading_days = provider.get_trading_days(
                    calendar_start, calendar_end, market=trading_config.market)
                for signal_day in entry_signal_days:
                    session = next(
                        (day for day in trading_days if day > signal_day), None)
                    if session is not None:
                        next_entry_session[signal_day] = session
            except Exception as exc:
                # Calendar failure blocks new entries but must not block risk
                # monitoring and exits for existing positions.
                calendar_error = str(exc)
        quotes = provider.get_live_quotes(codes) if codes else {}
        account = None
        broker_positions = []
        broker_order_map: dict[str, dict] = {}
        broker_state_loaded = False
        entry_reservations: dict[str, dict] = {}
        entry_reservation_error = ""

        def ensure_broker_state() -> None:
            nonlocal broker, account, broker_positions, broker_order_map
            nonlocal broker_state_loaded, entry_reservation_error
            if broker_state_loaded:
                return
            if broker is None:
                broker = MoomooBroker(trading_config, args.host, args.port)
            account = broker.account_snapshot(now)
            broker_positions = broker.positions()
            remote_orders = broker.orders()
            broker_order_map = {
                str(order.get("remark") or ""): order
                for order in remote_orders if order.get("remark")
            }
            log.record_account_snapshot(account.as_dict(), run_id)
            errors = []
            for client_id, remote in broker_order_map.items():
                if not _active_broker_order(remote):
                    continue
                local = log.broker_order(
                    trading_config.environment, account.account_id, client_id)
                # Only LongVol ENTRY orders consume a new-position slot.  A
                # recognizable remote entry without its frozen local intent is
                # unsafe to estimate, so all additional entries fail closed.
                looks_like_entry = client_id.startswith("LV-") and "-ENTRY-" in client_id
                if local is None:
                    if looks_like_entry:
                        errors.append(f"{client_id}: frozen intent missing")
                    continue
                if str(local.get("purpose") or "") != "ENTRY":
                    continue
                try:
                    intent_payload = json.loads(local.get("intent_json") or "{}")
                    signal_payload = ((intent_payload.get("metadata") or {})
                                      .get("signal") or {})
                    order_price = _finite_number(
                        remote.get("price") or remote.get("limit_price")
                    )
                    entry_price = order_price or float(intent_payload["limit_price"])
                    quantity = int(local.get("quantity") or intent_payload["quantity"])
                    planned_exit = _finite_number(
                        intent_payload.get("planned_option_exit_bid")
                    )
                    entry_reservations[client_id] = _entry_risk_reservation(
                        signal_payload, entry_price, quantity,
                        trading_config.estimated_fees_per_contract,
                        client_order_id=client_id,
                        planned_exit_bid=planned_exit,
                    )
                except Exception as exc:
                    errors.append(f"{client_id}: {exc}")
            entry_reservation_error = " | ".join(errors)
            broker_state_loaded = True

        if args.submit:
            ensure_broker_state()

        def execute(intent, option_quote, underlying_quote):
            nonlocal broker, account, broker_positions
            ensure_broker_state()
            assert broker is not None and account is not None
            return _submit_intent(
                broker, account, broker_positions, intent, option_quote,
                underlying_quote, trading_config, args, log, run_id,
            )

        def cancel_entry(signal: dict, option_code: str, reason: str) -> dict | None:
            ensure_broker_state()
            assert broker is not None
            client_id = _client_order_id(str(signal.get("as_of") or ""), "ENTRY", option_code)
            existing = broker_order_map.get(client_id)
            if not existing or not _active_broker_order(existing):
                return None
            broker_id = str(existing.get("order_id") or existing.get("orderID") or "")
            result = broker.cancel_order(
                broker_id, submit=args.submit,
                live_confirmation=getattr(args, "live_confirmation", ""),
            )
            log.event("WARNING", "PENDING_ENTRY_CANCELLED", reason, run_id,
                      {"client_order_id": client_id, "broker_order_id": broker_id})
            return result

        for signal in signals:
            symbol = str(signal.get("symbol") or "").upper()
            if signal.get("status") not in {"BUY_CANDIDATE", "PILOT_CANDIDATE"} or symbol in position_symbols:
                continue
            underlying_code = f"US.{symbol}"
            option_code = str(signal.get("selected_option") or "")
            try:
                signal_day = date.fromisoformat(str(signal.get("as_of") or "")[:10])
            except ValueError:
                row = {"symbol": symbol, "code": option_code,
                       "action": "CANCEL", "reason": "INVALID_SIGNAL_DATE"}
                cancelled = cancel_entry(signal, option_code,
                                         "INVALID_SIGNAL_DATE")
                if cancelled:
                    row["cancel_result"] = cancelled
                output.append(row)
                continue
            eligible_session = next_entry_session.get(signal_day)
            if calendar_error or eligible_session is None:
                row = {
                    "symbol": symbol, "code": option_code,
                    "action": "ALERT", "reason": "TRADING_CALENDAR_UNAVAILABLE",
                    "calendar_error": calendar_error or
                    "OpenD returned no session after the signal date",
                }
                cancelled = cancel_entry(
                    signal, option_code, "TRADING_CALENDAR_UNAVAILABLE")
                if cancelled:
                    row["cancel_result"] = cancelled
                output.append(row)
                continue
            if underlying_code not in quotes or option_code not in quotes:
                row = {"symbol": symbol, "code": option_code,
                       "action": "ALERT", "reason": "MISSING_LIVE_QUOTE"}
                cancelled = cancel_entry(signal, option_code, "MISSING_LIVE_QUOTE")
                if cancelled:
                    row["cancel_result"] = cancelled
                output.append(row)
                continue
            decision = evaluate_intraday_entry(
                signal, quotes[underlying_code], quotes[option_code], now,
                trading_config, next_eligible_session=eligible_session)
            row = {"symbol": symbol, **decision.as_dict()}
            after_window = (decision.reason == "OUTSIDE_ENTRY_WINDOW" and
                            now.time().replace(tzinfo=None) >
                            datetime.strptime(trading_config.entry_end_et, "%H:%M").time())
            if decision.action == "CANCEL" or decision.reason == "STALE_QUOTE" or after_window:
                cancelled = cancel_entry(signal, option_code, decision.reason)
                if cancelled:
                    row["cancel_result"] = cancelled
            if decision.action == "PLACE_ENTRY":
                intent = OrderIntent(
                    client_order_id=_client_order_id(signal["as_of"], "ENTRY", option_code),
                    code=option_code, side="BUY", quantity=decision.quantity,
                    limit_price=float(decision.limit_price), purpose="ENTRY",
                    multiplier=int(float(signal.get("selected_option_multiplier") or 100)),
                    underlying_code=underlying_code,
                    reference_spot=float(signal.get("current_price") or 0),
                    reference_option_ask=float(signal.get("selected_option_ask") or 0),
                    entry_atr=float(signal.get("atr") or 0),
                    hard_stop_spot=float(signal.get("entry_hard_stop_spot") or 0),
                    option_stop_price=float(signal.get("scenario_stop_option_price") or 0),
                    signal_date=signal["as_of"], metadata={
                        "signal": signal,
                        "submission_spot": quotes[underlying_code].last,
                        "submission_option_iv": quotes[option_code].iv,
                    },
                )
                row["intent"] = asdict(intent)
                row["order_result"] = execute(intent, quotes[option_code], quotes[underlying_code])
            output.append(row)

        for position in positions:
            underlying_code = f"US.{position.symbol}"
            option_code = position.option_symbol
            if underlying_code not in quotes or option_code not in quotes:
                output.append({"symbol": position.symbol, "code": option_code,
                               "action": "ALERT", "reason": "MISSING_LIVE_QUOTE"})
                continue
            decision = evaluate_intraday_exit(
                position, quotes[underlying_code], quotes[option_code], now, trading_config)
            row = {"symbol": position.symbol, **decision.as_dict()}
            if decision.action == "PLACE_EXIT":
                purpose = "RISK_EXIT" if decision.risk_exit else "TARGET_EXIT"
                purpose_stage = (
                    f"{purpose}-PARTIAL"
                    if purpose == "TARGET_EXIT" and
                    decision.quantity < position.contracts else
                    f"{purpose}-FINAL" if purpose == "TARGET_EXIT" else purpose
                )
                client_order_id = _client_order_id(
                    now.date().isoformat(), purpose_stage, option_code)
                if broker is not None and purpose == "RISK_EXIT":
                    conflicting = [order for remark, order in broker_order_map.items()
                                   if "TARGET_EXIT" in remark and
                                   str(order.get("code") or "") == option_code and
                                   _active_broker_order(order)]
                    if conflicting:
                        row["cancelled_target_orders"] = [broker.cancel_order(
                            str(order.get("order_id") or order.get("orderID") or ""),
                            submit=args.submit,
                            live_confirmation=getattr(args, "live_confirmation", ""),
                        ) for order in conflicting]
                        row["order_result"] = {
                            "submitted": False,
                            "deferred": True,
                            "reason": "WAIT_FOR_TARGET_ORDER_CANCELLATION",
                        }
                        output.append(row)
                        continue
                existing_exit = broker_order_map.get(client_order_id)
                if broker is not None and existing_exit and _active_broker_order(existing_exit):
                    if purpose == "RISK_EXIT":
                        remote_price = float(existing_exit.get("price") or 0)
                        if abs(remote_price - float(decision.limit_price)) <= .001:
                            row["order_result"] = {
                                "submitted": False, "duplicate": True,
                                "reason": "ACTIVE_RISK_EXIT_ALREADY_MARKETABLE",
                            }
                        else:
                            row["order_result"] = broker.modify_order_price(
                                str(existing_exit.get("order_id") or
                                    existing_exit.get("orderID") or ""),
                                decision.quantity, float(decision.limit_price),
                                submit=args.submit,
                                live_confirmation=getattr(args, "live_confirmation", ""),
                            )
                            log.event("WARNING", "RISK_EXIT_REPRICED",
                                      "active risk exit moved to the current executable bid",
                                      run_id, {"client_order_id": client_order_id,
                                               "price": decision.limit_price})
                    else:
                        row["order_result"] = {
                            "submitted": False, "duplicate": True,
                            "reason": "ACTIVE_TARGET_ORDER_ALREADY_EXISTS",
                        }
                    output.append(row)
                    continue
                intent = OrderIntent(
                    client_order_id=client_order_id,
                    code=option_code, side="SELL", quantity=decision.quantity,
                    limit_price=float(decision.limit_price), purpose=purpose,
                    multiplier=position.multiplier, underlying_code=underlying_code,
                    hard_stop_spot=position.hard_stop_spot,
                    metadata={"position": asdict(position), "reason": decision.reason,
                              "submission_spot": quotes[underlying_code].last,
                              "submission_option_iv": quotes[option_code].iv},
                )
                row["intent"] = asdict(intent)
                row["order_result"] = execute(intent, quotes[option_code], quotes[underlying_code])
            output.append(row)
        if broker is not None and account is not None:
            broker_orders = broker.orders()
            reconciled = sum(log.reconcile_broker_order(
                trading_config.environment, account.account_id, order)
                             for order in broker_orders)
            materialized = _materialize_broker_fills(
                log, run_id, trading_config.environment, account.account_id,
                args.positions, trading_config.estimated_fees_per_contract,
            )
            output.append({"action": "BROKER_RECONCILE", "reason": "ORDER_STATE_SYNC",
                           "orders_reconciled": reconciled,
                           "fills_materialized": materialized})
        write_json(args.output, {"observed_at": now.isoformat(), "decisions": output})
        log.finish_run(run_id)
        return output
    except Exception as exc:
        log.finish_run(run_id, "FAILED", str(exc))
        raise
    finally:
        log.close(); provider.close()
        if broker:
            broker.close()


def intraday_monitor(args) -> None:
    from .broker import load_broker_config
    while True:
        output = _intraday_once(args)
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        if not args.loop:
            return
        poll = load_broker_config(args.trading_config).monitor_poll_seconds
        sleep(poll)


def _fee_configuration_check(strategy_config, sizing_payload: dict,
                             trading_config) -> dict:
    cold = sizing_payload.get("cold_start") or {}
    validated = sizing_payload.get("validated_kelly") or {}
    values = {
        "strategy_per_side": float(strategy_config.option_fee_per_contract),
        "sizing_cold_start_per_side": float(cold.get("fees_per_contract")),
        "sizing_validated_per_side": float(validated.get("fees_per_contract")),
        "trading_estimate_per_side": float(trading_config.estimated_fees_per_contract),
    }
    reference = values["strategy_per_side"]
    ok = all(abs(value - reference) <= 1e-9 for value in values.values())
    return {
        "ok": ok,
        "values": values,
        "message": ("fee assumptions are consistent" if ok else
                    "strategy, sizing, and trading fee assumptions differ"),
    }


def healthcheck(args) -> None:
    from .news import provider_configuration
    research_key_present = bool(os.getenv("OPENAI_API_KEY"))
    checks: dict[str, object] = {
        "python": True,
        "data_dir": Path(args.data_dir).is_dir(),
        "state_dir_writable": os.access(Path(args.state_dir), os.W_OK) if Path(args.state_dir).exists() else True,
        "research_api_key_present": research_key_present,
        "research_configuration": {
            "ok": research_key_present,
            "model": os.getenv("OPENAI_RESEARCH_MODEL", "deepseek-flash"),
            "base_url": os.getenv("OPENAI_BASE_URL", ""),
            "native_web_search_opt_in": os.getenv(
                "LONGVOL_ALLOW_NATIVE_WEB_SEARCH", "").lower() in {"1", "true", "yes"},
            "capability_probe_run": False,
            "message": "configuration only; use research-capabilities --billable-probe to test tools",
        },
        "news_provider_configuration": provider_configuration(
            getattr(args, "news_providers", None)),
        "live_trading_enabled": False,
    }
    if args.opend or args.broker:
        from importlib.metadata import PackageNotFoundError, version
        try:
            moomoo_version = version("moomoo-api")
            protobuf_version = version("protobuf")
            checks["moomoo_dependencies"] = {
                "ok": (moomoo_version == "10.10.7008" and
                       protobuf_version == "5.29.5"),
                "moomoo_api": moomoo_version,
                "protobuf": protobuf_version,
                "required": "moomoo-api==10.10.7008, protobuf==5.29.5",
            }
        except PackageNotFoundError as exc:
            checks["moomoo_dependencies"] = {
                "ok": False,
                "message": f"missing Moomoo dependency: {exc}",
                "required": "moomoo-api==10.10.7008, protobuf==5.29.5",
            }
    try:
        from .broker import load_broker_config
        strategy_config = load_config(args.config)
        sizing_payload = read_json(args.sizing)
        trading_config_for_validation = load_broker_config(args.trading_config)
        fee_check = _fee_configuration_check(
            strategy_config, sizing_payload, trading_config_for_validation,
        )
        sizing_resolution = resolve_sizing(
            sizing_payload,
            equity=2500.0,
            entry_price=1.0,
            target_price=2.0,
            stop_price=0.5,
            multiplier=100,
            strategy_version=strategy_config.strategy_version,
            local_iv_observations=0,
            min_local_iv_observations=strategy_config.min_iv_history_observations,
            as_of=datetime.now(ZoneInfo("America/New_York")).date(),
            evidence_base_dir=Path(args.sizing).parent,
        )
        checks["strategy_and_sizing_config"] = {
            "ok": fee_check["ok"],
            "strategy_version": strategy_config.strategy_version,
            "default_mode": sizing_resolution.mode,
            "kelly_blockers": list(sizing_resolution.blockers),
            "fee_configuration": fee_check,
        }
    except Exception as exc:
        checks["strategy_and_sizing_config"] = {
            "ok": False, "message": str(exc),
        }
    if args.opend:
        from .moomoo_adapter import MoomooProvider
        provider = None
        try:
            provider = MoomooProvider(args.host, args.port)
            checks["opend"] = provider.healthcheck()
        except Exception as exc:
            checks["opend"] = {"ok": False, "message": str(exc)}
        finally:
            if provider:
                provider.close()
    if args.broker:
        from .broker import MoomooBroker, load_broker_config
        broker = None
        try:
            trading_config = load_broker_config(args.trading_config)
            broker = MoomooBroker(trading_config, args.host, args.port)
            snapshot = broker.account_snapshot(datetime.now(ZoneInfo("America/New_York")))
            checks["broker"] = {"ok": True, "environment": trading_config.environment,
                                "account_id": snapshot.account_id,
                                "strategy_equity": snapshot.strategy_equity,
                                "base_currency": snapshot.base_currency,
                                "equity_field": snapshot.equity_field,
                                "requested_equity_field": snapshot.requested_equity_field,
                                "equity_fallback_reason": snapshot.equity_fallback_reason}
            checks["live_trading_enabled"] = bool(
                trading_config.environment == "REAL" and trading_config.allow_live_orders)
        except Exception as exc:
            checks["broker"] = {"ok": False, "message": str(exc)}
        finally:
            if broker:
                broker.close()
    required_ok = bool(checks["data_dir"] and checks["state_dir_writable"] and
                       isinstance(checks.get("strategy_and_sizing_config"), dict) and
                       checks["strategy_and_sizing_config"].get("ok") and
                       (not args.research or checks["research_api_key_present"]) and
                       (not (args.opend or args.broker) or
                        isinstance(checks.get("moomoo_dependencies"), dict) and
                        checks["moomoo_dependencies"].get("ok")) and
                       (not args.opend or isinstance(checks.get("opend"), dict) and checks["opend"].get("ok")) and
                       (not args.broker or isinstance(checks.get("broker"), dict) and checks["broker"].get("ok")))
    result = {"ok": required_ok, "checks": checks}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not required_ok:
        raise SystemExit(1)


def export_log(args) -> None:
    db = sqlite3.connect(args.log_db)
    try:
        tables = [args.table] if args.table != "all" else ["runs", "candidate_snapshots", "feature_snapshots", "sizing_mode_decisions", "trades", "exit_decisions", "events", "account_snapshots", "broker_orders"]
        for table in tables:
            columns = [x[1] for x in db.execute(f"PRAGMA table_info({table})")]
            rows = [dict(zip(columns, row)) for row in db.execute(f"SELECT * FROM {table}")]
            if rows:
                write_rows(Path(args.output_dir) / f"{table}.csv", rows)
    finally:
        db.close()


def daily(args) -> None:
    """One-command after-close pipeline; deliberately stops before order entry."""
    from types import SimpleNamespace
    from .ops import exclusive_lock
    root = Path(args.root)
    state = root / "state"
    data = root / "data"
    with exclusive_lock(state / "daily.lock"):
        from .broker import load_broker_config
        strategy_config = load_config(args.config)
        sizing_payload = read_json(args.sizing)
        fee_check = _fee_configuration_check(
            strategy_config, sizing_payload,
            load_broker_config(args.trading_config),
        )
        if not fee_check["ok"]:
            raise ValueError(fee_check["message"])
        account_path = state / "broker" / "account.json"
        account_sync(SimpleNamespace(
            trading_config=args.trading_config, host=args.host, port=args.port,
            output=str(account_path),
            positions_out=str(state / "broker" / "positions.csv"),
            orders_out=str(state / "broker" / "orders.csv"),
            local_positions=str(data / "positions.csv"),
            log_db=str(state / "trading_log.sqlite3"),
        ))
        inbox = data / "candidate_inbox" / f"{args.as_of}.csv"
        screener(SimpleNamespace(as_of=args.as_of, output=str(inbox), config=args.config,
                                 host=args.host, port=args.port,
                                 log_db=str(state / "trading_log.sqlite3")))
        new_candidates = read_candidates(inbox)
        tracked_path = data / "tracked.csv"
        tracked_rows = []
        if tracked_path.exists():
            with open(tracked_path, newline="") as f:
                tracked_rows = list(csv.DictReader(f))
        positions_path = data / "positions.csv"
        open_positions = (read_positions(positions_path) if positions_path.exists() else [])
        protected_symbols = {p.symbol for p in open_positions if p.status == "OPEN" and p.contracts > 0}
        tracked_rows, candidate_rows = _merge_tracked_universe(
            tracked_rows, new_candidates, args.as_of, strategy_config.tracked_max_age_days,
            protected_symbols,
        )
        write_rows(tracked_path, tracked_rows)
        current_candidates = state / "candidates" / f"{args.as_of}.csv"
        write_rows(current_candidates, candidate_rows)
        candidates = read_candidates(current_candidates)
        symbols = ",".join(c.symbol for c in candidates)
        if not symbols:
            signal_path = state / "signals" / f"{args.as_of}.csv"
            exit_path = state / "exits" / f"{args.as_of}.csv"
            write_rows(signal_path, [], fieldnames=["symbol", "as_of", "status", "reasons"])
            write_rows(exit_path, [], fieldnames=["symbol", "option_symbol", "action", "reason"])
            print(json.dumps({"as_of": args.as_of, "status": "NO_CANDIDATES"}))
            return
        sync(SimpleNamespace(symbols=symbols, start=args.start, end=args.as_of, kind="all",
                             data_dir=str(data), config=args.config, host=args.host, port=args.port))
        fundamentals_path = data / "fundamentals.csv"
        existing_fundamentals = (read_fundamentals(fundamentals_path)
                                 if fundamentals_path.exists() else {})
        as_of_day = date.fromisoformat(args.as_of)
        news_start_day = (date.fromisoformat(getattr(args, "news_start", ""))
                          if getattr(args, "news_start", None) else
                          as_of_day - timedelta(days=int(getattr(args, "news_lookback_days", 14))))
        news_start, news_end = _news_window(news_start_day.isoformat(), args.as_of)
        providers, setup_diagnostics, provider_config = _configured_news_sources(
            getattr(args, "news_providers", None))
        local_packet_dir = root / "research_packets"
        screened_symbols = {candidate.symbol for candidate in new_candidates}
        news_outcomes = []
        for candidate in candidates:
            existing = existing_fundamentals.get(candidate.symbol)
            packet: dict = {}
            packet_path = state / "news" / "packets" / args.as_of / f"{candidate.symbol}.json"
            try:
                packet, packet_path = _sync_news_symbol(
                    candidate.symbol, news_start, news_end, state, providers,
                    setup_diagnostics, local_packet_dir,
                )
                age = ((as_of_day - existing.as_of).days
                       if existing and existing.as_of else None)
                stale = age is None or age < 0 or age > strategy_config.max_fundamental_age_days
                shock_day = None
                bars_path = data / "bars" / f"{candidate.symbol}.csv"
                if bars_path.exists():
                    from .metrics import panic_metrics
                    panic = panic_metrics(
                        read_bars(bars_path), strategy_config.min_volume_zscore,
                        strategy_config.min_range_atr, strategy_config.max_close_location,
                    )
                    shock_day = (date.fromisoformat(str(panic["recent_shock_day"]))
                                 if panic.get("recent_shock_day") else None)
                shock = bool(
                    shock_day and
                    (not existing or not existing.as_of or shock_day > existing.as_of)
                )
                # If bar diagnostics are unavailable after synchronization, a
                # newly screened name still receives one conservative review.
                shock = shock or bool(not bars_path.exists() and
                                      candidate.symbol in screened_symbols)
                packet_changed = bool(
                    not existing or
                    existing.news_packet_sha256 != packet.get("packet_sha256", "")
                )
                refresh = shock or packet_changed or stale
                if not refresh:
                    news_outcomes.append({
                        "symbol": candidate.symbol, "ok": True, "refreshed": False,
                        "packet": str(packet_path), "packet_sha256": packet["packet_sha256"],
                    })
                    continue
                broad_collection_ok = any(
                    row.get("ok") is True and row.get("gate_eligible") is True and
                    row.get("broad_news_coverage") is True
                    for row in packet.get("provider_diagnostics", [])
                    if isinstance(row, dict)
                )
                dated_output = state / "fundamentals" / args.as_of / f"{candidate.symbol}.json"
                if broad_collection_ok and packet.get("articles"):
                    result = research(SimpleNamespace(
                        symbol=candidate.symbol, packet=str(packet_path),
                        output=str(dated_output), fundamentals_out=str(fundamentals_path),
                        model=getattr(args, "research_model", None),
                        no_web_search=not bool(getattr(args, "allow_native_web_search", False)),
                        log_db=str(state / "trading_log.sqlite3"), as_of=args.as_of,
                    ))
                else:
                    result = _fundamental_fail_closed(
                        candidate.symbol, args.as_of, packet,
                        "No successfully queried gate-eligible broad-news source with PIT evidence.",
                    )
                    write_json(dated_output, result)
                    merge_fundamental(fundamentals_path, candidate.symbol, result, args.as_of)
                result["news_packet_path"] = str(packet_path)
                write_json(dated_output, result)
                write_json(state / "fundamentals" / f"{candidate.symbol}.json", result)
                existing_fundamentals = read_fundamentals(fundamentals_path)
                news_outcomes.append({
                    "symbol": candidate.symbol, "ok": True, "refreshed": True,
                    "shock": shock, "shock_day": shock_day.isoformat() if shock_day else None,
                    "stale": stale, "packet_changed": packet_changed,
                    "packet": str(packet_path), "packet_sha256": packet["packet_sha256"],
                    "thesis_status": result["thesis_status"],
                    "retrieval_mode": result["retrieval_mode"],
                })
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                result = _fundamental_fail_closed(candidate.symbol, args.as_of, packet, reason)
                result["news_packet_path"] = str(packet_path) if packet else ""
                dated_output = state / "fundamentals" / args.as_of / f"{candidate.symbol}.json"
                write_json(dated_output, result)
                write_json(state / "fundamentals" / f"{candidate.symbol}.json", result)
                merge_fundamental(fundamentals_path, candidate.symbol, result, args.as_of)
                existing_fundamentals = read_fundamentals(fundamentals_path)
                news_outcomes.append({
                    "symbol": candidate.symbol, "ok": False, "refreshed": True,
                    "packet": str(packet_path) if packet else "", "error": reason,
                    "thesis_status": "uncertain",
                })
        write_json(state / "news" / "daily" / f"{args.as_of}.json", {
            "as_of": args.as_of,
            "provider_configuration": provider_config,
            "outcomes": news_outcomes,
        })
        signal_path = state / "signals" / f"{args.as_of}.csv"
        scan(SimpleNamespace(candidates=str(current_candidates), bars_dir=str(data / "bars"),
                             intraday_dir=str(data / "intraday"), options_dir=str(data / "options"),
                             option_snapshot_root=str(data),
                             option_history=str(data / "option_history.csv"),
                             option_regime=str(data / "option_regime.csv"), short=str(data / "short.csv"),
                             fundamentals=str(data / "fundamentals.csv"), sizing=args.sizing,
                             account_state=str(account_path),
                             positions=str(data / "positions.csv"),
                             log_db=str(state / "trading_log.sqlite3"), as_of=args.as_of,
                             output=str(signal_path), config=args.config))
        positions = data / "positions.csv"
        if positions.exists():
            exits(SimpleNamespace(positions=str(positions), signals=str(signal_path), bars_dir=str(data / "bars"),
                                  options_dir=str(data / "options"), output=str(state / "exits" / f"{args.as_of}.csv"),
                                  log_db=str(state / "trading_log.sqlite3"), as_of=args.as_of, config=args.config))


def sync(args) -> None:
    """Materialize a bounded Moomoo/OpenD snapshot into the CSV contract."""
    from .moomoo_adapter import MoomooProvider
    root = Path(args.data_dir); symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if not symbols:
        raise ValueError("at least one symbol is required")
    if start > end:
        raise ValueError("start must be on or before end")
    config = load_config(getattr(args, "config", None)) if getattr(args, "config", None) else None
    intraday_days = config.max_intraday_calendar_days if config else 90
    intraday_start = max(start, end - timedelta(days=intraday_days))
    provider = MoomooProvider(args.host, args.port); short_rows = []; errors = []; warnings = []
    history_quota = None
    try:
        if args.kind in {"bars", "intraday", "all"}:
            history_quota = provider.get_history_quota()
            pulled = set(history_quota["codes"])
            requested = {symbol if "." in symbol else f"US.{symbol}" for symbol in symbols}
            new_codes = sorted(requested - pulled)
            if len(new_codes) > int(history_quota["remaining"]):
                raise RuntimeError(
                    "insufficient historical K-line quota before synchronization: "
                    f"need {len(new_codes)}, remaining {history_quota['remaining']}"
                )
            history_quota = {**history_quota, "new_codes_required": len(new_codes)}
        regime_by_symbol = {}
        if args.kind in {"options", "all"}:
            try:
                regime_by_symbol = provider.get_option_regimes(
                    [symbol if "." in symbol else f"US.{symbol}" for symbol in symbols], end)
            except Exception as exc:
                warnings.append(f"option regime: {exc}")
        for symbol in symbols:
            code = symbol if "." in symbol else f"US.{symbol}"
            stage = "initialization"
            try:
                if args.kind in {"bars", "all"}:
                    stage = "daily_bars"
                    bars = provider.get_bars(code, start, end)
                    if not bars:
                        raise RuntimeError("OpenD returned no daily bars")
                    write_rows(root / "bars" / f"{symbol}.csv", [{"day": b.day.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume} for b in bars])
                if args.kind in {"intraday", "all"}:
                    stage = "intraday_bars"
                    intraday = provider.get_intraday_bars(code, intraday_start, end)
                    if not intraday:
                        raise RuntimeError("OpenD returned no intraday bars")
                    write_rows(root / "intraday" / f"{symbol}.csv", [{"day": b.day.isoformat(),
                               "time": b.timestamp.isoformat(sep=" ") if b.timestamp else "",
                               "open": b.open, "high": b.high, "low": b.low,
                               "close": b.close, "volume": b.volume} for b in intraday])
                if args.kind in {"options", "all"}:
                    stage = "option_screening"
                    if config is None:
                        raise RuntimeError("strategy config is required for option screening")
                    options = provider.get_options(code, end, config)
                    if not options:
                        warnings.append(
                            f"{symbol}: no contracts passed the option-screen union; "
                            "the symbol will fail the option-data gate"
                        )
                    option_columns = [
                        "symbol", "expiry", "strike", "right", "bid", "ask", "last",
                        "iv", "delta", "gamma", "open_interest", "volume",
                        "iv_percentile", "quote_time", "multiplier", "vega", "theta",
                    ]
                    write_rows(
                        root / "options" / f"{symbol}.csv",
                        [{**o.__dict__, "expiry": o.expiry.isoformat()} for o in options],
                        fieldnames=option_columns,
                    )
                    stage = "option_snapshot_archive"
                    archive_option_snapshot(root, symbol, end, options)
                    if symbol not in regime_by_symbol:
                        warnings.append(f"{symbol}: Moomoo returned no dated option regime snapshot")
                if args.kind in {"short", "all"}:
                    stage = "short_data"
                    current = provider.get_short_snapshot(code, start, end)
                    if current:
                        short_rows.append({"symbol": symbol, "day": current.day.isoformat(),
                                           "short_interest": current.short_interest if current.short_interest is not None else "",
                                           "short_interest_5d_ago": current.short_interest_5d_ago if current.short_interest_5d_ago is not None else "",
                                           "short_interest_date": current.short_interest_date or "",
                                           "previous_short_interest_date": current.previous_short_interest_date or "",
                                           "short_volume_ratio": current.short_volume_ratio if current.short_volume_ratio is not None else "",
                                           "short_volume_ratio_20d_avg": current.short_volume_ratio_20d_avg if current.short_volume_ratio_20d_avg is not None else "",
                                           "short_volume_date": current.short_volume_date or ""})
            except Exception as exc:
                errors.append(f"{symbol} [{stage}]: {exc}")
        if short_rows:
            short_path = root / "short.csv"
            existing_short = []
            if short_path.exists():
                with open(short_path, newline="") as f:
                    existing_short = list(csv.DictReader(f))
            by_symbol = {r.get("symbol", "").upper(): r for r in existing_short}
            by_symbol.update({r["symbol"]: r for r in short_rows})
            write_rows(short_path, list(by_symbol.values()))
        if regime_by_symbol:
            regime_path = root / "option_regime.csv"
            existing_regimes = []
            if regime_path.exists():
                with open(regime_path, newline="") as f:
                    existing_regimes = list(csv.DictReader(f))
            new_regimes = [{
                "symbol": item.symbol, "day": item.day.isoformat(),
                "iv": item.iv if item.iv is not None else "",
                "iv_rank": item.iv_rank if item.iv_rank is not None else "",
                "iv_percentile": item.iv_percentile if item.iv_percentile is not None else "",
                "iv_change": item.iv_change if item.iv_change is not None else "",
                "hv": item.hv if item.hv is not None else "",
                "hv_change": item.hv_change if item.hv_change is not None else "",
                "put_call_volume_ratio": (item.put_call_volume_ratio
                                          if item.put_call_volume_ratio is not None else ""),
                "put_call_open_interest_ratio": (item.put_call_open_interest_ratio
                                                  if item.put_call_open_interest_ratio is not None else ""),
                "source": item.source,
            } for item in regime_by_symbol.values()]
            by_key = {(r.get("symbol", "").upper(), r.get("day", "")): r
                      for r in existing_regimes}
            by_key.update({(r["symbol"], r["day"]): r for r in new_regimes})
            write_rows(regime_path, sorted(by_key.values(),
                                          key=lambda r: (r["symbol"], r["day"])))
    finally:
        provider.close()
    result = {"symbols": symbols, "kind": args.kind, "history_quota": history_quota,
              "warnings": warnings, "errors": errors}
    print(json.dumps(result, indent=2))
    if errors:
        raise RuntimeError("Moomoo sync failed: " + "; ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(prog="longvol")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scan"); p.add_argument("--candidates", required=True); p.add_argument("--bars-dir", required=True); p.add_argument("--intraday-dir"); p.add_argument("--options-dir"); p.add_argument("--option-snapshot-root"); p.add_argument("--option-history"); p.add_argument("--option-regime"); p.add_argument("--short"); p.add_argument("--fundamentals"); p.add_argument("--sizing"); p.add_argument("--account-state"); p.add_argument("--positions", required=True); p.add_argument("--config", default="config/strategy.json"); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.add_argument("--as-of"); p.add_argument("--output", required=True); p.set_defaults(func=scan)
    p = sub.add_parser("exits"); p.add_argument("--positions", required=True); p.add_argument("--signals", required=True); p.add_argument("--bars-dir", required=True); p.add_argument("--options-dir"); p.add_argument("--config", default="config/strategy.json"); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.add_argument("--as-of"); p.add_argument("--output", required=True); p.set_defaults(func=exits)
    p = sub.add_parser("screener"); p.add_argument("--as-of", required=True); p.add_argument("--output", required=True); p.add_argument("--config", default="config/strategy.json"); p.add_argument("--host", default=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1")); p.add_argument("--port", type=int, default=int(os.getenv("MOOMOO_OPEND_PORT", "11111"))); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.set_defaults(func=screener)
    p = sub.add_parser("research"); p.add_argument("--symbol", required=True); p.add_argument("--packet", required=True); p.add_argument("--as-of", default=date.today().isoformat()); p.add_argument("--output", required=True); p.add_argument("--fundamentals-out"); p.add_argument("--model"); p.add_argument("--no-web-search", action="store_true"); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.set_defaults(func=research)
    p = sub.add_parser("research-capabilities", help="run an explicit billable model tool probe"); p.add_argument("--billable-probe", action="store_true", required=True); p.add_argument("--model"); p.set_defaults(func=research_capabilities)
    p = sub.add_parser("news-sync"); p.add_argument("--symbols", required=True, help="comma-separated tickers"); p.add_argument("--start", required=True); p.add_argument("--as-of", required=True); p.add_argument("--state-dir", default="state"); p.add_argument("--providers", default=os.getenv("LONGVOL_NEWS_PROVIDERS", "auto")); p.add_argument("--local-packet-dir", default="research_packets"); p.set_defaults(func=news_sync)
    p = sub.add_parser("record-trade"); p.add_argument("--trade-json", required=True); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.add_argument("--positions-out"); p.set_defaults(func=record_trade)
    p = sub.add_parser("account-sync"); p.add_argument("--trading-config", default="config/trading.json"); p.add_argument("--output", default="state/broker/account.json"); p.add_argument("--positions-out", default="state/broker/positions.csv"); p.add_argument("--orders-out", default="state/broker/orders.csv"); p.add_argument("--local-positions", default="data/positions.csv"); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.add_argument("--host", default=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1")); p.add_argument("--port", type=int, default=int(os.getenv("MOOMOO_OPEND_PORT", "11111"))); p.set_defaults(func=account_sync)
    p = sub.add_parser("place-order"); p.add_argument("--intent", required=True); p.add_argument("--trading-config", default="config/trading.json"); p.add_argument("--submit", action="store_true"); p.add_argument("--live-confirmation", default=""); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.add_argument("--host", default=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1")); p.add_argument("--port", type=int, default=int(os.getenv("MOOMOO_OPEND_PORT", "11111"))); p.set_defaults(func=place_order)
    p = sub.add_parser("intraday-monitor"); p.add_argument("--signals", required=True); p.add_argument("--positions", default="data/positions.csv"); p.add_argument("--trading-config", default="config/trading.json"); p.add_argument("--output", default="state/intraday/latest.json"); p.add_argument("--submit", action="store_true"); p.add_argument("--live-confirmation", default=""); p.add_argument("--loop", action="store_true"); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.add_argument("--host", default=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1")); p.add_argument("--port", type=int, default=int(os.getenv("MOOMOO_OPEND_PORT", "11111"))); p.set_defaults(func=intraday_monitor)
    p = sub.add_parser("sync"); p.add_argument("--symbols", required=True, help="comma-separated tickers, e.g. AAPL,MSFT"); p.add_argument("--start", required=True); p.add_argument("--end", required=True); p.add_argument("--kind", choices=["bars", "intraday", "options", "short", "all"], default="all"); p.add_argument("--data-dir", default="data"); p.add_argument("--config", default="config/strategy.json"); p.add_argument("--host", default=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1")); p.add_argument("--port", type=int, default=int(os.getenv("MOOMOO_OPEND_PORT", "11111"))); p.set_defaults(func=sync)
    p = sub.add_parser("healthcheck"); p.add_argument("--data-dir", default="data"); p.add_argument("--state-dir", default="state"); p.add_argument("--opend", action="store_true"); p.add_argument("--broker", action="store_true"); p.add_argument("--research", action="store_true"); p.add_argument("--news-providers", default=os.getenv("LONGVOL_NEWS_PROVIDERS", "auto")); p.add_argument("--config", default="config/strategy.json"); p.add_argument("--sizing", default="config/sizing.json"); p.add_argument("--trading-config", default="config/trading.json"); p.add_argument("--host", default=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1")); p.add_argument("--port", type=int, default=int(os.getenv("MOOMOO_OPEND_PORT", "11111"))); p.set_defaults(func=healthcheck)
    p = sub.add_parser("export-log"); p.add_argument("--log-db", default="state/trading_log.sqlite3"); p.add_argument("--table", choices=["all", "runs", "candidate_snapshots", "feature_snapshots", "sizing_mode_decisions", "trades", "exit_decisions", "events", "account_snapshots", "broker_orders"], default="all"); p.add_argument("--output-dir", required=True); p.set_defaults(func=export_log)
    p = sub.add_parser("daily"); p.add_argument("--as-of", required=True); p.add_argument("--start", required=True); p.add_argument("--root", default="."); p.add_argument("--config", default="config/strategy.json"); p.add_argument("--sizing", default="config/sizing.json"); p.add_argument("--trading-config", default="config/trading.json"); p.add_argument("--host", default=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1")); p.add_argument("--port", type=int, default=int(os.getenv("MOOMOO_OPEND_PORT", "11111"))); p.add_argument("--research-model"); p.add_argument("--news-providers", default=os.getenv("LONGVOL_NEWS_PROVIDERS", "auto")); p.add_argument("--news-start"); p.add_argument("--news-lookback-days", type=int, default=14); p.add_argument("--allow-native-web-search", action="store_true"); p.set_defaults(func=daily)
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        log_path = getattr(args, "log_db", None)
        if not log_path and args.command == "daily":
            log_path = str(Path(args.root) / "state" / "trading_log.sqlite3")
        if log_path:
            try:
                failure_log = TradingLog(log_path)
                failure_log.event("ERROR", "COMMAND_FAILED", str(exc),
                                  details={"command": args.command, "exception_type": type(exc).__name__})
                failure_log.close()
            except Exception:
                pass
        raise


if __name__ == "__main__": main()
