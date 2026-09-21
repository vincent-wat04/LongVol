from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from longvol.config import (
    OPTION_MC_EVIDENCE_INTENDED_USE,
    OPTION_MC_EVIDENCE_SCHEMA_VERSION,
    load_config,
    option_mc_implementation_sha256,
    option_mc_spec_sha256,
)
from longvol.kelly import FixedRiskInput, size_fixed_risk
from longvol.models import Bar, Candidate, Config, OptionQuote
from longvol.option_simulation import (
    ContractFilter,
    EconomicPolicy,
    RegimeState,
    SimulationSettings,
    UnderlyingDistribution,
    build_reversal_distribution,
    rank_option_contracts,
    simulate_option,
)
from longvol.strategy import _select_option_with_mc, evaluate


AS_OF = date(2026, 9, 9)


def option(
    symbol: str = "TEST261218C00100000",
    *,
    strike: float = 100.0,
    bid: float = 7.50,
    ask: float = 8.00,
    iv: float = 0.60,
    delta: float = 0.55,
    open_interest: float = 1_000,
    volume: float = 100,
) -> OptionQuote:
    return OptionQuote(
        symbol=symbol,
        expiry=AS_OF + timedelta(days=100),
        strike=strike,
        right="C",
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        iv=iv,
        delta=delta,
        gamma=0.03,
        open_interest=open_interest,
        volume=volume,
        quote_time=datetime.combine(AS_OF, datetime.min.time()),
    )


def distribution(rebound_probability: float = 0.50) -> UnderlyingDistribution:
    return build_reversal_distribution(
        spot=100.0,
        atr=4.0,
        target_spot=110.0,
        hard_stop_spot=94.0,
        horizon_days=10,
        annualized_volatility=0.45,
        rebound_probability=rebound_probability,
        continuation_probability=0.25,
    )


def oos_config_payload(
    directory: str,
    *,
    config_overrides: dict | None = None,
    manifest_overrides: dict | None = None,
) -> tuple[dict, Path]:
    """Create a valid, self-consistent OOS manifest and Config payload."""

    payload = asdict(Config())
    payload.update({
        "option_mc_mode": "OOS_GATE",
        "option_mc_assumption_source": "study:option-mc-v1",
        "option_mc_out_of_sample_validated": True,
        "option_mc_validation_sample_end": "2026-08-31",
        "option_mc_validation_observation_days": 252,
        "option_mc_validation_independent_events": 100,
    })
    payload.update(config_overrides or {})
    provisional = Config(**payload)
    manifest = {
        "schema_version": OPTION_MC_EVIDENCE_SCHEMA_VERSION,
        "approved": True,
        "out_of_sample": True,
        "strategy_version": provisional.strategy_version,
        "assumption_source": provisional.option_mc_assumption_source,
        "intended_use": OPTION_MC_EVIDENCE_INTENDED_USE,
        "spec_sha256": option_mc_spec_sha256(provisional),
        "implementation_sha256": option_mc_implementation_sha256(),
        "sample_start": "2025-01-01",
        "feature_end": "2026-08-15",
        "label_end": provisional.option_mc_validation_sample_end,
        "parameter_frozen_at": "2024-12-31T23:00:00+00:00",
        "approved_at": "2026-09-01T12:00:00+00:00",
        "observation_days": provisional.option_mc_validation_observation_days,
        "independent_events": provisional.option_mc_validation_independent_events,
        "parameter_trials": 12,
        "validation_folds": 5,
        "purge_trading_days": provisional.option_mc_horizon_trading_days,
        "embargo_trading_days": provisional.option_mc_horizon_trading_days,
    }
    manifest.update(manifest_overrides or {})
    manifest_path = Path(directory) / "option-mc-evidence.json"
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    with open(manifest_path, "wb") as handle:
        handle.write(manifest_bytes)
    payload["option_mc_validation_study_path"] = str(manifest_path)
    payload["option_mc_validation_study_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    return payload, manifest_path


class OptionSimulationTests(unittest.TestCase):
    def test_same_seed_is_exactly_reproducible(self):
        settings = SimulationSettings(paths=1_000, seed=81)
        first = simulate_option(option(), AS_OF, distribution(), settings)
        second = simulate_option(option(), AS_OF, distribution(), settings)
        self.assertEqual(first, second)

    def test_stock_condition_probability_changes_call_distribution(self):
        settings = SimulationSettings(paths=5_000, seed=7)
        low_rebound = simulate_option(option(), AS_OF, distribution(0.20), settings)
        high_rebound = simulate_option(option(), AS_OF, distribution(0.70), settings)
        self.assertGreater(high_rebound.expected_return, low_rebound.expected_return)
        self.assertGreater(high_rebound.probability_profit, low_rebound.probability_profit)

    def test_flat_path_loses_to_ask_bid_cost_and_theta(self):
        flat = UnderlyingDistribution(
            spot=100.0,
            atr=1.0,
            target_spot=110.0,
            hard_stop_spot=90.0,
            horizon_days=5,
            annualized_volatility=0.0001,
            states=(RegimeState("FLAT", 1.0, 100.0, 1.0, 1.0),),
        )
        result = simulate_option(
            option(bid=7.50, ask=8.00),
            AS_OF,
            flat,
            SimulationSettings(paths=100, seed=1, annualized_iv_noise=0.0),
        )
        self.assertLess(result.expected_return, 0)
        self.assertEqual(result.probability_profit, 0)
        self.assertGreaterEqual(result.exit_haircut, 1 - 7.50 / 7.75)

    def test_reports_tail_risk_and_separates_capital_from_planned_risk(self):
        result = simulate_option(
            option(ask=6.00, bid=5.50),
            AS_OF,
            distribution(),
            SimulationSettings(paths=3_000, seed=19,
                               premium_stop_loss_fraction=0.55),
        )
        self.assertEqual(result.full_premium_capital_risk, 600.0)
        self.assertAlmostEqual(result.planned_risk_per_contract,
                               -result.stop_scenario.pnl_per_contract)
        self.assertEqual(result.modeled_stop_loss_per_contract,
                         result.planned_risk_per_contract)
        self.assertAlmostEqual(result.operational_premium_stop_price, 2.70)
        self.assertEqual(result.operational_premium_stop_loss_per_contract,
                         330.0)
        self.assertGreaterEqual(result.cvar_loss_per_contract, result.var_loss_per_contract)
        self.assertAlmostEqual(
            result.cvar_capital_fraction,
            result.cvar_loss_per_contract / result.full_premium_capital_risk,
        )
        self.assertLessEqual(result.p05_return, result.p95_return)
        exit_probability = (
            result.target_exit_probability
            + result.stop_exit_probability
            + result.premium_stop_exit_probability
            + result.time_exit_probability
        )
        self.assertAlmostEqual(exit_probability, 1.0)

    def test_target_and_stop_scenarios_are_executable_not_mid_marks(self):
        quote = option(bid=5.00, ask=6.00)
        result = simulate_option(
            quote,
            AS_OF,
            distribution(),
            SimulationSettings(paths=500, seed=3, minimum_exit_haircut=0.05,
                               stressed_exit_haircut=0.20),
        )
        self.assertGreater(
            result.target_scenario.theoretical_option_price,
            result.target_scenario.executable_option_price,
        )
        self.assertGreater(
            result.stop_scenario.theoretical_option_price,
            result.stop_scenario.executable_option_price,
        )
        self.assertGreater(result.target_scenario.pnl_per_contract,
                           result.stop_scenario.pnl_per_contract)

    def test_near_worthless_exit_keeps_full_exit_fee_in_planned_risk(self):
        quote = option(strike=200.0, bid=0.001, ask=0.01, iv=0.15)
        settings = SimulationSettings(
            paths=100, seed=13, annualized_iv_noise=0.0,
            entry_fee_per_contract=2.0, exit_fee_per_contract=2.0,
        )
        result = simulate_option(quote, AS_OF, distribution(), settings)
        self.assertLess(
            result.stop_scenario.executable_option_price * quote.multiplier,
            settings.exit_fee_per_contract,
        )
        expected_loss = (
            (quote.ask - result.stop_scenario.executable_option_price) * quote.multiplier
            + settings.entry_fee_per_contract + settings.exit_fee_per_contract
        )
        self.assertAlmostEqual(result.modeled_stop_loss_per_contract, expected_loss)
        sizing = size_fixed_risk(FixedRiskInput(
            equity=100_000,
            entry_price=quote.ask,
            target_price=0.20,
            stop_price=result.stop_scenario.executable_option_price,
            fees_per_contract=2.0,
        ))
        self.assertAlmostEqual(sizing.risk_per_contract, expected_loss)

    def test_ranking_runs_every_liquid_contract_and_uses_economics(self):
        # EXPENSIVE has the tighter quote but is deliberately overpriced versus
        # CHEAP, which has otherwise identical payoff exposure.
        expensive = option("EXPENSIVE", bid=9.90, ask=10.00)
        cheap = option("CHEAP", bid=5.00, ask=5.50)
        illiquid = option("NO_OI", bid=5.20, ask=5.50, open_interest=2)
        kwargs = dict(
            as_of=AS_OF,
            distribution=distribution(0.65),
            settings=SimulationSettings(paths=3_000, seed=41),
            contract_filter=ContractFilter(
                min_dte=30,
                max_dte=180,
                min_exit_dte=5,
                min_abs_delta=0.20,
                max_abs_delta=0.80,
                min_open_interest=100,
                min_volume=10,
                max_spread_pct=0.20,
                max_premium_to_spot=0.20,
                require_quote_on_as_of=True,
            ),
            economic_policy=EconomicPolicy(
                min_expected_return=None,
                min_probability_profit=None,
                min_target_scenario_return=None,
            ),
        )
        ranking = rank_option_contracts([expensive, illiquid, cheap], **kwargs)
        reverse = rank_option_contracts([cheap, illiquid, expensive], **kwargs)
        self.assertEqual([item.option.symbol for item in ranking.ranked],
                         [item.option.symbol for item in reverse.ranked])
        self.assertEqual(ranking.ranked[0].option.symbol, "CHEAP")
        self.assertEqual(ranking.rejected[0].symbol, "NO_OI")
        self.assertIn("OPEN_INTEREST", ranking.rejected[0].reasons)

    def test_default_economic_policy_can_fail_without_dropping_result(self):
        flat = UnderlyingDistribution(
            spot=100.0,
            atr=1.0,
            target_spot=110.0,
            hard_stop_spot=90.0,
            horizon_days=5,
            annualized_volatility=0.0001,
            states=(RegimeState("FLAT", 1.0, 100.0, 1.0, 1.0),),
        )
        ranking = rank_option_contracts(
            [option()],
            AS_OF,
            flat,
            SimulationSettings(paths=100, seed=1, annualized_iv_noise=0.0),
        )
        self.assertEqual(len(ranking.ranked), 1)
        self.assertFalse(ranking.ranked[0].passes_economic_policy)
        self.assertIn("EXPECTED_RETURN", ranking.ranked[0].economic_reasons)

    def test_production_policy_can_fail_closed_on_unvalidated_distribution(self):
        ranking = rank_option_contracts(
            [option()],
            AS_OF,
            distribution(),
            SimulationSettings(paths=100, seed=1),
            economic_policy=EconomicPolicy(
                min_expected_return=None,
                min_probability_profit=None,
                min_target_scenario_return=None,
                require_oos_distribution=True,
            ),
        )
        self.assertFalse(ranking.ranked[0].passes_economic_policy)
        self.assertIn("DISTRIBUTION_NOT_OOS_VALIDATED",
                      ranking.ranked[0].economic_reasons)

    def test_research_selection_ignores_probability_bearing_outputs(self):
        candidate = Candidate("TEST", AS_OF)
        quote = option(bid=5.0, ask=5.5, delta=.40)
        base = Config(
            min_option_open_interest=0,
            min_option_volume=0,
            target_delta_low=.20,
            target_delta_high=.80,
            max_spread_pct=.20,
            option_mc_paths=1_000,
            option_mc_mode="RESEARCH_ONLY",
            # Impossible MC floors must be ignored in research mode.
            option_mc_min_expected_return=10.0,
            option_mc_min_probability_profit=1.0,
            option_mc_min_robust_score=10.0,
        )
        low = _select_option_with_mc(
            [quote], candidate, 100.0, 4.0, 110.0, 94.0, .45,
            replace(base, option_mc_rebound_probability=.10,
                    option_mc_continuation_probability=.60),
        )
        high = _select_option_with_mc(
            [quote], candidate, 100.0, 4.0, 110.0, 94.0, .45,
            replace(base, option_mc_rebound_probability=.80,
                    option_mc_continuation_probability=.10),
        )
        self.assertEqual(low[0].symbol, quote.symbol)
        self.assertEqual(high[0].symbol, quote.symbol)
        self.assertTrue(low[5])
        self.assertTrue(high[5])
        self.assertNotEqual(low[3]["option_mc_expected_return"],
                            high[3]["option_mc_expected_return"])
        self.assertFalse(low[3]["option_mc_economic_policy_applied"])

    def test_research_selection_ranks_payoff_to_loss_before_spread(self):
        tight = option("TIGHT", bid=7.90, ask=8.00, delta=.40)
        wider_but_better = option("BETTER", bid=5.00, ask=5.50, delta=.40)
        config = Config(
            min_option_open_interest=0,
            min_option_volume=0,
            target_delta_low=.20,
            target_delta_high=.80,
            max_spread_pct=.20,
            option_mc_paths=500,
        )
        selected = _select_option_with_mc(
            [tight, wider_but_better], Candidate("TEST", AS_OF),
            100.0, 4.0, 110.0, 94.0, .45, config,
        )
        self.assertEqual(selected[0].symbol, "BETTER")
        self.assertGreater(wider_but_better.spread_pct, tight.spread_pct)
        self.assertEqual(
            selected[3]["option_mc_selection_rule"],
            "DETERMINISTIC_TARGET_PAYOFF_TO_LOSS_THEN_SPREAD",
        )

    def test_oos_mode_applies_mc_economic_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, _manifest = oos_config_payload(tmp, config_overrides={
                "min_option_open_interest": 0,
                "min_option_volume": 0,
                "target_delta_low": .20,
                "target_delta_high": .80,
                "max_spread_pct": .20,
                "option_mc_paths": 500,
                "option_mc_min_expected_return": 10.0,
                "option_mc_min_probability_profit": 1.0,
                "option_mc_min_robust_score": 10.0,
            })
            config = Config(**payload)
            result = _select_option_with_mc(
                [option(bid=5.0, ask=5.5, delta=.40)],
                Candidate("TEST", AS_OF), 100.0, 4.0, 110.0, 94.0, .45,
                config,
            )
        self.assertIsNone(result[0])
        self.assertTrue(result[4])
        self.assertFalse(result[5])
        self.assertTrue(result[3]["option_mc_economic_policy_applied"])
        self.assertEqual(result[3]["option_mc_contracts_policy_passed"], 0)

    def test_config_rejects_unvalidated_oos_gate(self):
        payload = asdict(Config())
        payload["option_mc_mode"] = "OOS_GATE"
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/strategy.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(ValueError, "out-of-sample validated"):
                load_config(path)

    def test_config_accepts_sourced_validated_oos_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, study = oos_config_payload(tmp)
            payload["option_mc_validation_study_path"] = study.name
            path = f"{tmp}/strategy.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            loaded = load_config(path)
        self.assertEqual(loaded.option_mc_mode, "OOS_GATE")
        self.assertTrue(loaded.option_mc_out_of_sample_validated)
        self.assertEqual(Path(loaded.option_mc_validation_study_path), study.resolve())

    def test_config_rejects_arbitrary_hashed_file_as_oos_evidence(self):
        payload = asdict(Config())
        payload.update({
            "option_mc_mode": "OOS_GATE",
            "option_mc_assumption_source": "study:not-a-manifest",
            "option_mc_out_of_sample_validated": True,
            "option_mc_validation_sample_end": "2026-08-31",
            "option_mc_validation_observation_days": 252,
            "option_mc_validation_independent_events": 100,
        })
        with tempfile.TemporaryDirectory() as tmp:
            study = Path(tmp) / "arbitrary.json"
            body = b'{"out_of_sample":true}'
            with open(study, "wb") as handle:
                handle.write(body)
            payload["option_mc_validation_study_path"] = str(study)
            payload["option_mc_validation_study_sha256"] = hashlib.sha256(body).hexdigest()
            config_path = Path(tmp) / "strategy.json"
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(ValueError, "manifest schema mismatch"):
                load_config(config_path)

    def test_direct_config_cannot_bypass_oos_file_hash(self):
        config = Config(
            option_mc_mode="OOS_GATE",
            option_mc_assumption_source="study:fake",
            option_mc_out_of_sample_validated=True,
            option_mc_validation_study_path="does-not-exist.json",
            option_mc_validation_study_sha256="a" * 64,
            option_mc_validation_sample_end="2026-08-31",
            option_mc_validation_observation_days=252,
            option_mc_validation_independent_events=100,
        )
        with self.assertRaisesRegex(ValueError, "file does not exist"):
            _select_option_with_mc(
                [option()], Candidate("TEST", AS_OF),
                100.0, 4.0, 110.0, 94.0, .45, config,
            )

    def test_oos_manifest_semantic_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, _manifest = oos_config_payload(
                tmp, manifest_overrides={"assumption_source": "study:other"},
            )
            config_path = Path(tmp) / "strategy.json"
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(ValueError, "assumption_source mismatch"):
                load_config(config_path)

    def test_oos_manifest_implementation_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, _manifest = oos_config_payload(
                tmp, manifest_overrides={"implementation_sha256": "0" * 64},
            )
            config_path = Path(tmp) / "strategy.json"
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(ValueError, "implementation SHA-256 mismatch"):
                load_config(config_path)

    def test_oos_manifest_rejects_parameter_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, _manifest = oos_config_payload(tmp)
            payload["option_mc_rebound_probability"] = 0.41
            config_path = Path(tmp) / "strategy.json"
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(ValueError, "spec SHA-256 mismatch"):
                load_config(config_path)

    def test_oos_cutoff_must_be_strictly_before_signal(self):
        for cutoff in (AS_OF, AS_OF + timedelta(days=1)):
            with self.subTest(cutoff=cutoff), tempfile.TemporaryDirectory() as tmp:
                payload, _manifest = oos_config_payload(
                    tmp,
                    config_overrides={
                        "option_mc_validation_sample_end": cutoff.isoformat(),
                    },
                    manifest_overrides={
                        "feature_end": (cutoff - timedelta(days=1)).isoformat(),
                        "label_end": cutoff.isoformat(),
                        "approved_at": (
                            cutoff + timedelta(days=1)
                        ).isoformat() + "T12:00:00+00:00",
                    },
                )
                config = Config(**payload)
                with self.assertRaisesRegex(ValueError, "strictly before the signal"):
                    _select_option_with_mc(
                        [option()], Candidate("TEST", AS_OF),
                        100.0, 4.0, 110.0, 94.0, .45, config,
                    )

    def test_oos_governance_minima_cannot_be_lowered(self):
        payload = asdict(Config())
        payload["option_mc_min_validation_observation_days"] = 251
        payload["option_mc_min_validation_independent_events"] = 99
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "strategy.json"
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaisesRegex(ValueError, "governance minimum"):
                load_config(config_path)

    def test_strategy_emits_mc_scenario_and_exposure_metadata(self):
        start = AS_OF - timedelta(days=79)
        daily = [
            Bar(start + timedelta(days=index), 100.0, 101.0, 99.0,
                100.0, 100.0)
            for index in range(80)
        ]
        evaluation = evaluate(
            Candidate("TEST", AS_OF),
            daily,
            [option(bid=2.40, ask=2.60, delta=.40)],
            None,
            None,
            Config(
                min_history_bars=20,
                min_option_open_interest=0,
                min_option_volume=0,
                option_mc_paths=500,
            ),
        )
        self.assertIsNotNone(evaluation.selected_option)
        self.assertTrue(evaluation.hard_gates["option_economics"])
        self.assertEqual(evaluation.metrics["target_source"], "ATR_FALLBACK")
        self.assertEqual(evaluation.metrics["scenario_horizon_unit"], "TRADING_DAYS")
        self.assertEqual(evaluation.metrics["strategy_horizon"], "SHORT_TERM_REVERSAL")
        self.assertEqual(
            evaluation.metrics["instrument_exposure"],
            "LONG_CALL_LONG_DELTA_LONG_GAMMA_LONG_VEGA_NEGATIVE_THETA",
        )
        self.assertEqual(
            evaluation.metrics["option_mc_role"],
            "DIAGNOSTICS_ONLY_PROBABILITY_FREE_DETERMINISTIC_GATE",
        )
        self.assertIsNotNone(evaluation.metrics["option_mc_expected_return"])
        self.assertFalse(evaluation.metrics["option_mc_economic_policy_applied"])
        expected_modeled_loss = (
            (2.60 - evaluation.metrics["scenario_stop_option_price"]) * 100 + 4.0
        )
        self.assertAlmostEqual(
            evaluation.metrics["option_mc_modeled_stop_loss_per_contract"],
            expected_modeled_loss,
        )
        self.assertAlmostEqual(
            evaluation.metrics["option_mc_planned_risk_per_contract"],
            expected_modeled_loss,
        )
        self.assertEqual(evaluation.metrics["option_mc_full_premium_capital_risk"],
                         262.0)
        self.assertAlmostEqual(
            evaluation.metrics["option_mc_operational_premium_stop_loss_per_contract"],
            2.60 * 100 * .55 + 4.0,
        )
        self.assertAlmostEqual(
            evaluation.metrics["option_mc_operational_premium_stop_price"],
            2.60 * (1.0 - .55),
        )
        self.assertEqual(
            evaluation.metrics["option_mc_planned_option_exit_bid"],
            evaluation.metrics["scenario_stop_option_price"],
        )
        sizing = size_fixed_risk(FixedRiskInput(
            equity=100_000,
            entry_price=2.60,
            target_price=evaluation.metrics["scenario_target_option_price"],
            stop_price=evaluation.metrics["scenario_stop_option_price"],
            fees_per_contract=2.0,
        ))
        self.assertAlmostEqual(
            sizing.risk_per_contract,
            evaluation.metrics["option_mc_planned_risk_per_contract"],
        )
        ranking = json.loads(evaluation.metrics["option_mc_ranking_summary_json"])
        self.assertEqual(ranking[0]["symbol"], evaluation.selected_option.symbol)

    def test_iv_regime_and_surface_are_observable_opt_in_gates(self):
        start = AS_OF - timedelta(days=79)
        daily = [
            Bar(start + timedelta(days=index), 100.0, 101.0, 99.0,
                100.0, 100.0)
            for index in range(80)
        ]
        base = dict(
            min_history_bars=20,
            min_option_open_interest=0,
            min_option_volume=0,
            option_mc_paths=100,
        )
        observed_only = evaluate(
            Candidate("TEST", AS_OF), daily,
            [option(bid=2.40, ask=2.60, delta=.40)], None, None,
            Config(**base),
        )
        self.assertFalse(observed_only.metrics["iv_regime_observed"])
        self.assertFalse(observed_only.metrics["option_surface_observed"])
        self.assertTrue(observed_only.hard_gates["iv_regime"])
        self.assertTrue(observed_only.hard_gates["option_surface"])
        self.assertFalse(observed_only.metrics["iv_regime_required"])
        self.assertFalse(observed_only.metrics["option_surface_required"])

        required = evaluate(
            Candidate("TEST", AS_OF), daily,
            [option(bid=2.40, ask=2.60, delta=.40)], None, None,
            Config(**base, require_iv_regime_gate=True,
                   require_option_surface_gate=True),
        )
        self.assertFalse(required.hard_gates["iv_regime"])
        self.assertFalse(required.hard_gates["option_surface"])


if __name__ == "__main__":
    unittest.main()
