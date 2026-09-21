import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from longvol.broker import (AccountSnapshot, BrokerConfig, LiveQuote, MoomooBroker,
                            OrderIntent, normalize_account_snapshot,
                            validate_live_order)
from longvol.cli import (_broker_position_quantity, _fee_configuration_check,
                         _materialize_broker_fills,
                         _merge_tracked_universe, record_trade, sync)
from longvol.exits import evaluate_exit
from longvol.io import read_options, read_positions
from longvol.kelly import KellyInput, size_position
from longvol.kelly import FixedRiskInput, size_fixed_risk
from longvol.intraday import evaluate_intraday_entry, evaluate_intraday_exit
from longvol.data_quality import validate_bars, validate_options, validate_profile_bars
from longvol.metrics import (compression_metrics, gamma_walls,
                             option_surface_metrics, panic_metrics,
                             residual_return_metrics, short_pressure,
                             volume_profile)
from longvol.moomoo_adapter import (MoomooProvider, normalize_history_quota,
                                    normalize_option_quote, normalize_option_regime,
                                    normalize_stock_screen_item,
                                    normalize_trading_days)
from longvol.portfolio import check_portfolio_capacity
from longvol.models import (Bar, Candidate, Config, Evaluation,
                            FundamentalSnapshot, OptionQuote,
                            OptionRegimeSnapshot, Position, ShortSnapshot)
from longvol.strategy import evaluate
from longvol.trading_log import TradingLog
from longvol.readiness import resolve_sizing
from longvol.session import account_snapshot_matches_session, resolve_daily_as_of


def bars(prices, volumes=None, start=date(2026, 1, 1)):
    volumes = volumes or [100.0] * len(prices)
    out = []
    for i, (price, vol) in enumerate(zip(prices, volumes)):
        out.append(Bar(start + timedelta(days=i), price, price * 1.01, price * .99, price, vol))
    return out


def quote(as_of, bid=2.4, ask=2.6, iv=.60, symbol="A261218C00100000"):
    return OptionQuote(symbol, as_of + timedelta(days=90), 100, "C", bid, ask,
                       (bid + ask) / 2, iv, .45, .03, 1000, 100,
                       quote_time=datetime.combine(as_of, datetime.min.time()))


class MetricsTests(unittest.TestCase):
    def test_compression_requires_post_shock_volume_decay(self):
        prices = []
        price = 100.0
        for i in range(30):
            price += 3 if i % 2 == 0 else -3
            prices.append(price)
        for i in range(10):
            price += .2 if i % 2 == 0 else -.2
            prices.append(price)
        start = date(2026, 1, 1)

        def sample(post_shock_volume):
            volumes = [100.0] * 34 + [1000.0] + [post_shock_volume] * 5
            return [
                Bar(start + timedelta(days=i), value, value * 1.005,
                    value * .995, value, volume)
                for i, (value, volume) in enumerate(zip(prices, volumes))
            ]

        expanding = compression_metrics(sample(1200), shock_index=34)
        self.assertEqual(expanding["volatility_compression_count"], 2)
        self.assertFalse(expanding["selling_pressure_compressed"])
        self.assertFalse(expanding["raw_compressed"])

        exhausted = compression_metrics(sample(500), shock_index=34)
        self.assertAlmostEqual(exhausted["post_shock_volume_ratio"], .5)
        self.assertTrue(exhausted["selling_pressure_compressed"])
        self.assertTrue(exhausted["raw_compressed"])

    def test_panic_metrics_anchor_last_same_bar_shock(self):
        start = date(2026, 1, 1)
        data = [
            Bar(start + timedelta(days=i), 100, 101, 99, 100,
                100 + (i % 3) * 10)
            for i in range(42)
        ]
        shock_day = start + timedelta(days=42)
        data.append(Bar(shock_day, 98, 99, 79, 80, 1000))
        data.append(Bar(start + timedelta(days=43), 81, 82, 80, 81, 400))
        data.append(Bar(start + timedelta(days=44), 82, 83, 81, 82, 300))
        result = panic_metrics(data)
        self.assertTrue(result["recent_shock_event"])
        self.assertEqual(result["recent_shock_day"], shock_day.isoformat())
        self.assertEqual(result["sessions_since_recent_shock"], 2)

    def test_panic_metrics_do_not_combine_shock_and_volume_from_different_days(self):
        start = date(2026, 1, 1)
        data = [
            Bar(start + timedelta(days=i), 100, 101, 99, 100,
                100 + (i % 3) * 10)
            for i in range(42)
        ]
        data.append(Bar(start + timedelta(days=42), 98, 99, 79, 80, 100))
        data.append(Bar(start + timedelta(days=43), 80, 80.5, 79.5, 80, 1000))
        data.extend([
            Bar(start + timedelta(days=44), 80, 80.5, 79.5, 80, 100),
            Bar(start + timedelta(days=45), 80, 80.5, 79.5, 80, 100),
        ])
        result = panic_metrics(data)
        self.assertFalse(result["recent_shock_event"])
        self.assertIsNone(result["recent_shock_day"])

    def test_daily_bars_must_end_on_as_of_date(self):
        as_of = date(2026, 9, 8)
        data = bars([100] * 80, start=date(2026, 6, 20))
        result = validate_bars(data, as_of, Config())
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["bars_exact_as_of"])

    def test_moomoo_option_normalization_scales_percent_iv_and_delta(self):
        result = normalize_option_quote({"code": "US.A", "strike_time": "2026-12-18",
                                         "strike_price": 100, "option_type": "CALL",
                                         "bid_price": 2, "ask_price": 2.2, "last_price": 2.1,
                                         "option_implied_volatility": 60, "option_delta": 45,
                                         "option_gamma": .03, "option_open_interest": 1000,
                                         "volume": 100})
        self.assertAlmostEqual(result.iv, .60)
        self.assertAlmostEqual(result.delta, .45)

    def test_moomoo_option_normalization_skips_na_screen_placeholders(self):
        result = normalize_option_quote({
            "code": "US.BRZE261120C35000",
            "strike_date": "N/A",
            "strike_time": "2026-11-20",
            "strike_price": float("nan"),
            "option_strike_price": 35.0,
            "option_type": "CALL",
            "bid_price": .15,
            "ask_price": .40,
            "last_price": .30,
            "option_implied_volatility": 61.057,
            "option_delta": 10.08,
            "option_gamma": .02755,
            "option_open_interest": 3872,
            "volume": 146,
            "update_time": "2026-09-09 15:49:54",
        })
        self.assertEqual(result.expiry, date(2026, 11, 20))
        self.assertEqual(result.strike, 35.0)
        self.assertEqual(result.right, "C")
        self.assertEqual(result.quote_time, datetime(2026, 9, 9, 15, 49, 54))

    def test_moomoo_option_screen_normalization_handles_numeric_fields(self):
        observed = datetime(2026, 9, 8, 17, 15)
        result = normalize_option_quote({
            "code": "US.AAPL261218C00200000", "strike_date": 20261218,
            "strike_price": 200, "option_type": 1, "bid_price": 4.8,
            "ask_price": 5.0, "price": 4.9, "implied_volatility": 42,
            "delta": .45, "gamma": .02, "open_interest": 1000, "volume": 50,
        }, observed)
        self.assertEqual(result.right, "C")
        self.assertEqual(result.expiry, date(2026, 12, 18))
        self.assertAlmostEqual(result.iv, .42)
        self.assertEqual(result.quote_time, observed)

    def test_moomoo_stock_screen_v2_normalization(self):
        item = {"stock_id": 1, "results": [
            {"property": {"name": 1101}, "value_type": 1, "sval": "AAPL"},
            {"property": {"name": 2201}, "value_type": 4, "dval": 200.0},
            {"property": {"name": 3102}, "value_type": 4, "dval": -.12},
        ]}
        result = normalize_stock_screen_item(item, {"symbol": 1101, "price": 2201, "change": 3102})
        self.assertEqual(result, {"symbol": "AAPL", "price": 200.0, "change": -.12})

    def test_moomoo_option_regime_normalizes_vendor_percent_fields(self):
        result = normalize_option_regime({
            "code": "US.AAPL", "trading_date": "2026-09-09",
            "iv": 42.5, "iv_rank": 63.0, "iv_percentile": 72.0,
            "iv_change": -8.0, "hv": 35.0, "hv_change": 4.0,
            "volume_ratio": .8, "open_interest_ratio": 1.2,
        }, date(2026, 9, 9))
        self.assertEqual(result.symbol, "AAPL")
        self.assertAlmostEqual(result.iv, .425)
        self.assertAlmostEqual(result.iv_percentile, .72)
        self.assertAlmostEqual(result.iv_change, -.08)

    def test_moomoo_option_regime_accepts_epoch_timestamp(self):
        timestamp = int(datetime(
            2026, 9, 9, 16, 0, tzinfo=ZoneInfo("America/New_York")
        ).timestamp())
        result = normalize_option_regime({
            "code": "US.AAPL", "timestamp": timestamp,
            "iv": 42.5, "iv_rank": 63.0, "iv_percentile": 72.0,
        }, date(2026, 9, 9))
        self.assertEqual(result.day, date(2026, 9, 9))

    def test_history_quota_normalizes_both_sdk_shapes(self):
        flat = normalize_history_quota((2, 98, {"code": "US.AAPL"}, {"code": "US.MSFT"}))
        nested = normalize_history_quota((2, 98, [{"code": "US.AAPL"}, {"code": "US.MSFT"}]))
        self.assertEqual(flat, nested)
        self.assertEqual(flat, {"used": 2, "remaining": 98,
                                "codes": ["US.AAPL", "US.MSFT"]})

    def test_trading_calendar_normalizes_unique_open_sessions(self):
        result = normalize_trading_days([
            {"time": "2026-09-11", "trade_date_type": "WHOLE"},
            {"time": "2026-09-11", "trade_date_type": "AFTERNOON"},
            {"time": "2026-09-14", "trade_date_type": "WHOLE"},
            {"time": "invalid"},
        ], date(2026, 9, 10), date(2026, 9, 14))
        self.assertEqual(result, [date(2026, 9, 11), date(2026, 9, 14)])

    def test_provider_healthcheck_rejects_opend_too_old_for_option_screening(self):
        provider = object.__new__(MoomooProvider)
        provider._ctx = SimpleNamespace(get_global_state=object())
        provider._call = lambda _method: (0, {"server_ver": "1007"})
        result = provider.healthcheck()
        self.assertFalse(result["ok"])
        self.assertEqual(result["server_ver"], "1007")
        self.assertIn("Option Screening V2", result["message"])

    def test_provider_healthcheck_accepts_matching_opend(self):
        provider = object.__new__(MoomooProvider)
        provider._ctx = SimpleNamespace(get_global_state=object())
        provider._call = lambda _method: (0, {"server_ver": "1010"})
        result = provider.healthcheck()
        self.assertTrue(result["ok"])
        self.assertEqual(result["required_opend"], "10.10.7008")

    def test_provider_reads_us_trading_calendar(self):
        provider = object.__new__(MoomooProvider)
        provider._ctx = SimpleNamespace(request_trading_days=object())
        observed = {}

        def call(method, **kwargs):
            observed.update(kwargs)
            return 0, [{"time": "2026-09-11", "trade_date_type": "WHOLE"},
                       {"time": "2026-09-14", "trade_date_type": "WHOLE"}]

        provider._call = call
        fake_moomoo = SimpleNamespace(
            TradeDateMarket=SimpleNamespace(US="US"))
        with patch.dict("sys.modules", {"moomoo": fake_moomoo}):
            result = provider.get_trading_days(
                date(2026, 9, 10), date(2026, 9, 14), market="US")
        self.assertEqual(result, [date(2026, 9, 11), date(2026, 9, 14)])
        self.assertEqual(observed["start"], "2026-09-10")
        self.assertEqual(observed["end"], "2026-09-14")

    def test_sync_checks_history_quota_before_writing(self):
        class Provider:
            def __init__(self, *_args):
                pass

            def get_history_quota(self):
                return {"used": 100, "remaining": 0, "codes": []}

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp, patch(
                "longvol.moomoo_adapter.MoomooProvider", Provider):
            args = SimpleNamespace(symbols="AAPL", start="2026-01-01", end="2026-09-08",
                                   kind="bars", data_dir=tmp, config=None,
                                   host="127.0.0.1", port=11111)
            with self.assertRaisesRegex(RuntimeError, "insufficient historical K-line quota"):
                sync(args)
            self.assertFalse((Path(tmp) / "bars" / "AAPL.csv").exists())

    def test_sync_materializes_empty_option_file_without_aborting_batch(self):
        class Provider:
            def __init__(self, *_args):
                pass

            def get_history_quota(self):
                return {"used": 1, "remaining": 99, "codes": ["US.UI"]}

            def get_option_regimes(self, _codes, _as_of):
                return {}

            def get_bars(self, _code, _start, _end):
                return bars([100, 101])

            def get_intraday_bars(self, _code, _start, _end):
                return bars([100, 101])

            def get_options(self, _code, _as_of, _config):
                return []

            def get_short_snapshot(self, _code, _start, _end):
                return None

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp, patch(
                "longvol.moomoo_adapter.MoomooProvider", Provider):
            args = SimpleNamespace(
                symbols="UI", start="2026-01-01", end="2026-09-09",
                kind="all", data_dir=tmp, config="config/strategy.json",
                host="127.0.0.1", port=11111,
            )
            sync(args)
            option_path = Path(tmp) / "options" / "UI.csv"
            self.assertTrue(option_path.exists())
            self.assertEqual(read_options(option_path), [])
            self.assertIn("symbol,expiry,strike,right", option_path.read_text())
            archived = Path(tmp) / "options_history" / "2026-09-09" / "UI.csv"
            self.assertTrue(archived.exists())
            self.assertEqual(read_options(archived), [])

    def test_option_screen_unions_surface_and_flow_then_refreshes_tradable_contracts(self):
        as_of = datetime.now(ZoneInfo("America/New_York")).date()
        expiry = (as_of + timedelta(days=90)).isoformat()
        update_time = f"{as_of.isoformat()} 16:05:00"
        surface = [{"code": "US.A1", "strike_date": expiry, "strike_price": 100,
                    "option_type": 1, "bid_price": 1.0, "ask_price": 1.2,
                    "implied_volatility": .5, "delta": .45, "gamma": .02,
                    "open_interest": 500, "volume": 20}]
        flow = [{"code": "US.A2", "strike_date": expiry, "strike_price": 105,
                 "option_type": 2, "bid_price": .9, "ask_price": 1.1,
                 "implied_volatility": .6, "delta": -.30, "gamma": .02,
                 "open_interest": 20, "volume": 500}]
        provider = MoomooProvider.__new__(MoomooProvider)
        provider._option_screen_rows = lambda code, config, flow: (flow_rows if flow else surface)
        refreshed = []
        def snapshots(codes):
            refreshed.extend(codes)
            return [{"code": "US.A1", "bid_price": 1.05, "ask_price": 1.15,
                     "option_implied_volatility": .48, "option_delta": .44,
                     "option_gamma": .021, "option_contract_size": 100,
                     "option_open_interest": 510, "volume": 25,
                     "update_time": update_time},
                    {"code": "US.A2", "bid_price": .92, "ask_price": 1.08,
                     "option_implied_volatility": .59, "option_delta": -.29,
                     "option_gamma": .019, "option_contract_size": 100,
                     "option_open_interest": 21, "volume": 510,
                     "update_time": update_time}]
        provider._market_snapshots = snapshots
        flow_rows = flow
        result = provider.get_options("US.A", as_of, Config())
        self.assertEqual({x.symbol for x in result}, {"US.A1", "US.A2"})
        self.assertEqual(refreshed, ["US.A1"])
        selected = next(x for x in result if x.symbol == "US.A1")
        self.assertAlmostEqual(selected.bid, 1.05)
        self.assertAlmostEqual(selected.iv, .48)
        self.assertEqual(selected.open_interest, 510)

    def test_option_screen_rejects_historical_as_of_and_oversized_union(self):
        provider = MoomooProvider.__new__(MoomooProvider)
        with self.assertRaises(ValueError):
            provider.get_options("US.A", date(2000, 1, 1), Config())
        as_of = datetime.now(ZoneInfo("America/New_York")).date()
        expiry = (as_of + timedelta(days=90)).isoformat()
        rows = [{"code": f"US.A{i}", "strike_date": expiry, "strike_price": 100 + i,
                 "option_type": 1, "bid_price": 1, "ask_price": 1.1,
                 "implied_volatility": .5, "delta": .45, "gamma": .02,
                 "open_interest": 500, "volume": 20} for i in range(2)]
        provider._option_screen_rows = lambda code, config, flow: rows if not flow else []
        provider._market_snapshots = lambda codes: []
        with self.assertRaises(RuntimeError):
            provider.get_options("US.A", as_of, Config(option_screener_max_contracts=1))

    def test_option_screen_rejects_unverifiable_snapshot_timestamp(self):
        as_of = date(2026, 9, 9)
        expiry = (as_of + timedelta(days=90)).isoformat()
        provider = MoomooProvider.__new__(MoomooProvider)
        rows = [{"code": "US.A1", "strike_date": expiry, "strike_price": 100,
                 "option_type": 1, "bid_price": 1, "ask_price": 1.1,
                 "implied_volatility": .5, "delta": .45, "gamma": .02,
                 "open_interest": 500, "volume": 20}]
        provider._option_screen_rows = lambda code, config, flow: rows if not flow else []
        provider._market_snapshots = lambda codes: [{"code": "US.A1"}]
        with patch("longvol.moomoo_adapter._now_et", return_value=datetime(
                2026, 9, 10, 4, 30, tzinfo=ZoneInfo("America/New_York"))):
            with self.assertRaisesRegex(RuntimeError, "no option Market Snapshot rows"):
                provider.get_options("US.A", as_of, Config())

    def test_preopen_option_catchup_refreshes_all_and_keeps_only_as_of(self):
        as_of = date(2026, 9, 9)
        expiry = (as_of + timedelta(days=90)).isoformat()
        rows = [{"code": f"US.A{i}", "strike_date": expiry, "strike_price": 100 + i,
                 "option_type": 1, "bid_price": 1, "ask_price": 1.1,
                 "implied_volatility": .5, "delta": .45 if i == 1 else .6,
                 "gamma": .02, "open_interest": 500, "volume": 20}
                for i in (1, 2)]
        provider = MoomooProvider.__new__(MoomooProvider)
        provider._option_screen_rows = lambda code, config, flow: rows if not flow else []
        refreshed = []
        def snapshots(codes):
            refreshed.extend(codes)
            return [
                {"code": "US.A1", "update_time": "2026-09-09 16:01:00"},
                {"code": "US.A2", "update_time": "2026-09-08 16:01:00"},
            ]
        provider._market_snapshots = snapshots
        with patch("longvol.moomoo_adapter._now_et", return_value=datetime(
                2026, 9, 10, 4, 30, tzinfo=ZoneInfo("America/New_York"))):
            result = provider.get_options("US.A", as_of, Config())
        self.assertEqual(refreshed, ["US.A1", "US.A2"])
        self.assertEqual([item.symbol for item in result], ["US.A1"])

    def test_option_quality_requires_every_valid_quote_on_as_of(self):
        as_of = date(2026, 9, 9)
        fresh = quote(as_of, symbol="FRESH")
        stale = quote(as_of, symbol="STALE")
        stale = OptionQuote(**{**stale.__dict__,
                               "quote_time": datetime(2026, 9, 8, 16, 0)})
        result = validate_options([fresh, stale], as_of, Config())
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["option_quotes_as_of"])

    def test_daily_session_guard_allows_explicit_preopen_catchup(self):
        now = datetime(2026, 9, 10, 4, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(resolve_daily_as_of(now, "2026-09-09"), date(2026, 9, 9))
        with self.assertRaisesRegex(ValueError, "before 17:00"):
            resolve_daily_as_of(now, None)

    def test_daily_session_guard_rejects_late_historical_catchup(self):
        now = datetime(2026, 9, 10, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        with self.assertRaisesRegex(ValueError, "before 09:00"):
            resolve_daily_as_of(now, "2026-09-09")

    def test_account_snapshot_accepts_only_same_day_or_next_preopen(self):
        et = ZoneInfo("America/New_York")
        as_of = date(2026, 9, 9)
        self.assertTrue(account_snapshot_matches_session(
            datetime(2026, 9, 9, 17, 5, tzinfo=et), as_of))
        self.assertTrue(account_snapshot_matches_session(
            datetime(2026, 9, 10, 8, 59, tzinfo=et), as_of))
        self.assertFalse(account_snapshot_matches_session(
            datetime(2026, 9, 10, 9, 0, tzinfo=et), as_of))

    def test_volume_profile_uses_range_distribution(self):
        data = [Bar(date(2026, 1, 1), 105, 105.1, 104.9, 105, 100),
                Bar(date(2026, 1, 2), 102, 102.1, 101.9, 102, 1000),
                Bar(date(2026, 1, 3), 100, 100.1, 99.9, 100, 100)]
        profile = volume_profile(data, bin_size=.25)
        self.assertAlmostEqual(profile.peak_price, 101.75, delta=.5)
        self.assertGreater(profile.overhead_share_10pct, 0)
        self.assertIsNotNone(profile.value_area_low)

    def test_gamma_wall_is_labelled_proxy_and_sorted(self):
        expiry = date(2026, 10, 16)
        chain = [OptionQuote("A", expiry, 100, "C", 1, 1.1, 1.05, .5, .4, .1, 1000),
                 OptionQuote("A", expiry, 105, "C", 1, 1.1, 1.05, .5, .4, .2, 1000)]
        walls = gamma_walls(chain, 100)
        self.assertEqual(walls[0]["strike"], 105)
        self.assertEqual(walls[0]["quality"], "oi_gamma_proxy")

    def test_gamma_wall_uses_contract_multiplier(self):
        expiry = date(2026, 10, 16)
        standard = OptionQuote("A", expiry, 100, "C", 1, 1.1, 1.05, .5, .4, .1, 1000)
        adjusted = OptionQuote("B", expiry, 105, "C", 1, 1.1, 1.05, .5, .4, .1, 1000,
                               multiplier=150)
        walls = gamma_walls([standard, adjusted], 100)
        self.assertEqual(walls[0]["strike"], 105)
        self.assertAlmostEqual(sum(float(w["abs_gex_share"]) for w in walls), 1.0)

    def test_option_quality_allows_valid_fresh_subset(self):
        as_of = date(2026, 1, 1)
        fresh = quote(as_of)
        stale = OptionQuote("BAD", as_of + timedelta(days=90), 100, "C", 2, 1, 1.5,
                            0, 2, -1, 0)
        result = validate_options([fresh, stale], as_of, Config())
        self.assertTrue(result.ok)

    def test_profile_rejects_future_data(self):
        as_of = date(2026, 1, 5)
        data = bars([100] * 100, start=date(2026, 1, 6))
        result = validate_profile_bars(data, as_of, Config(profile_min_bars=100))
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["profile_not_future"])

    def test_option_surface_calculates_skew_and_term_structure(self):
        as_of = date(2026, 1, 1)
        chain = [
            OptionQuote("A", as_of + timedelta(days=45), 100, "C", 1, 1.1, 1.05, .40, .25, .01, 100),
            OptionQuote("A", as_of + timedelta(days=45), 100, "P", 1, 1.1, 1.05, .50, -.25, .01, 100),
            OptionQuote("A", as_of + timedelta(days=45), 100, "C", 1, 1.1, 1.05, .42, .50, .01, 100),
            OptionQuote("A", as_of + timedelta(days=120), 100, "C", 2, 2.1, 2.05, .45, .50, .01, 100),
        ]
        result = option_surface_metrics(chain, 100, as_of)
        self.assertAlmostEqual(result["put_call_25d_skew"], .10)
        self.assertIsNotNone(result["term_structure_slope"])

    def test_short_flow_and_short_interest_are_separate(self):
        s = ShortSnapshot(date(2026, 1, 1), 90, 100, .40, None,
                          short_volume_ratio_20d_avg=.50)
        result = short_pressure(s)
        self.assertTrue(result["short_interest_weaker"])
        self.assertTrue(result["short_flow_weaker"])
        self.assertTrue(result["weakening"])

    def test_residual_returns_require_explicit_benchmark_inputs(self):
        data = bars([100, 99, 98, 97, 96, 90])
        unavailable = residual_return_metrics(data, {"screen_change_5d": -.10})
        self.assertAlmostEqual(unavailable["stock_return_5d"], -.10)
        self.assertIsNone(unavailable["market_residual_return_5d"])
        self.assertFalse(unavailable["market_residual_available"])

        available = residual_return_metrics(data, {
            "screen_change_5d": -.10,
            "screen_market_change_5d": -.02,
            "screen_sector_change_5d": -.03,
            "screen_market_beta": 1.25,
            "screen_sector_beta": 1.5,
        })
        self.assertAlmostEqual(available["market_residual_return_5d"], -.075)
        self.assertAlmostEqual(available["sector_residual_return_5d"], -.055)

    def test_residual_return_falls_back_to_local_bars_not_zero_benchmark(self):
        data = bars([100, 99, 98, 97, 96, 90])
        result = residual_return_metrics(data)
        self.assertAlmostEqual(result["stock_return_5d"], -.10)
        self.assertEqual(result["stock_return_5d_source"], "local_daily_bars")
        self.assertIsNone(result["market_return_5d"])


class SizingTests(unittest.TestCase):
    def test_kelly_rejects_non_positive_edge_and_caps_risk_and_premium(self):
        bad = size_position(KellyInput(10000, .20, 2.0, 3.0, 1.5))
        self.assertFalse(bad.eligible)
        good = size_position(KellyInput(100000, .60, 2.0, 5.0, 1.0,
                                        max_allocation=.01, max_premium_allocation=.02))
        self.assertTrue(good.eligible)
        self.assertLessEqual(good.applied_fraction, .01)
        self.assertLessEqual(good.premium_fraction, .02)

    def test_portfolio_gate_caps_total_premium_and_duplicate_underlying(self):
        position = Position("TEST", "TEST-C", date(2026, 8, 1), 5.0, 100.0, 10,
                            250.0, date(2026, 12, 1))
        capped = check_portfolio_capacity([position], "NEW", 100_000,
                                          5_100, 100, .10, .10, 8, 1)
        self.assertFalse(capped.eligible)
        duplicate = check_portfolio_capacity([position], "TEST", 100_000,
                                             100, 100, .20, .20, 8, 1)
        self.assertFalse(duplicate.eligible)
        allowed = check_portfolio_capacity([position], "NEW", 100_000,
                                           1_000, 100, .20, .20, 8, 1)
        self.assertTrue(allowed.eligible)

    def test_small_account_profile_can_size_one_liquid_contract(self):
        result = size_position(KellyInput(
            2500, .60, 1.0, 2.5, .40, fees_per_contract=2,
            fractional_kelly=.25, max_allocation=.03,
            max_premium_allocation=.08, max_contracts=2,
        ))
        self.assertTrue(result.eligible)
        self.assertEqual(result.contracts, 1)
        self.assertLessEqual(result.capital_required, 200)
        self.assertAlmostEqual(result.risk_per_contract, 64)
        self.assertAlmostEqual(result.capital_at_risk_per_contract, 102)

    def test_cold_start_sizes_without_a_probability(self):
        result = size_fixed_risk(FixedRiskInput(
            2500, 1.0, 2.5, .45, fees_per_contract=2,
            max_risk_allocation=.025, max_premium_allocation=.05,
            max_contracts=1,
        ))
        self.assertTrue(result.eligible)
        self.assertEqual(result.contracts, 1)
        self.assertEqual(result.mode, "COLD_START_FIXED_RISK")
        self.assertIsNone(result.edge)
        self.assertAlmostEqual(result.risk_per_contract, 59)
        self.assertAlmostEqual(result.capital_at_risk_per_contract, 102)

    def test_full_premium_tail_cap_is_independent_of_tight_planned_stop(self):
        result = size_fixed_risk(FixedRiskInput(
            2500, 1.5, 3.0, 1.45, fees_per_contract=2,
            max_risk_allocation=.025, max_premium_allocation=.05,
            max_contracts=1,
        ))
        self.assertFalse(result.eligible)
        self.assertAlmostEqual(result.risk_per_contract, 9)
        self.assertAlmostEqual(result.capital_at_risk_per_contract, 152)

    def test_portfolio_reports_planned_and_full_premium_risk_separately(self):
        position = Position(
            "TEST", "TEST-C", date(2026, 8, 1), 2.0, 100.0, 2,
            75.0, date(2026, 12, 1),
            capital_at_risk_per_contract=202.0,
        )
        result = check_portfolio_capacity(
            [position], "NEW", 10_000, 302, 80, .20, .20, 8, 1,
        )
        self.assertEqual(result.committed_planned_risk, 150)
        self.assertEqual(result.proposed_planned_risk, 80)
        self.assertEqual(result.committed_full_premium_tail_risk, 404)
        self.assertEqual(result.proposed_full_premium_tail_risk, 302)

    def test_fee_assumptions_must_match_across_all_execution_layers(self):
        sizing = {
            "cold_start": {"fees_per_contract": 2.0},
            "validated_kelly": {"fees_per_contract": 2.0},
        }
        trading = SimpleNamespace(estimated_fees_per_contract=2.0)
        self.assertTrue(_fee_configuration_check(Config(), sizing, trading)["ok"])
        sizing["validated_kelly"]["fees_per_contract"] = 2.5
        self.assertFalse(_fee_configuration_check(Config(), sizing, trading)["ok"])

    def test_auto_sizing_stays_cold_until_evidence_and_iv_are_complete(self):
        payload = {
            "mode": "AUTO",
            "cold_start": {"fees_per_contract": 2, "max_risk_allocation": .025,
                           "max_premium_allocation": .05, "max_contracts": 1,
                           "max_portfolio_premium_allocation": .05,
                           "max_portfolio_risk_allocation": .025,
                           "max_open_positions": 1,
                           "max_positions_per_underlying": 1},
            "validated_kelly": {
                "win_probability_lower_bound": None, "fees_per_contract": 2,
                "fractional_kelly": .25, "max_risk_allocation": .03,
                "max_premium_allocation": .08, "max_contracts": 2,
                "max_portfolio_premium_allocation": .20,
                "max_portfolio_risk_allocation": .09,
                "max_open_positions": 3,
                "max_positions_per_underlying": 1,
                "min_oos_observation_days": 60, "min_oos_completed_trades": 30,
                "evidence": {"approved": False, "out_of_sample": False,
                             "strategy_version": "0.6.0", "study_sha256": None,
                             "sample_end": None, "observation_days": 0,
                             "completed_trades": 0},
            },
        }
        result = resolve_sizing(
            payload, equity=2500, entry_price=1, target_price=2.5,
            stop_price=.45, multiplier=100, strategy_version="0.6.0",
            local_iv_observations=10, min_local_iv_observations=60,
            as_of=date(2026, 9, 9),
        )
        self.assertEqual(result.mode, "COLD_START_FIXED_RISK")
        self.assertIn("LOCAL_IV_HISTORY_INCOMPLETE", result.blockers)

    def test_auto_sizing_switches_only_with_approved_versioned_oos_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "study.json"
            report.write_text('{"study":"point-in-time"}', encoding="utf-8")
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            payload = {
                "mode": "AUTO",
                "cold_start": {"fees_per_contract": 2, "max_risk_allocation": .025,
                               "max_premium_allocation": .05, "max_contracts": 1,
                               "max_portfolio_premium_allocation": .05,
                               "max_portfolio_risk_allocation": .025,
                               "max_open_positions": 1,
                               "max_positions_per_underlying": 1},
                "validated_kelly": {
                    "win_probability_lower_bound": .55, "fees_per_contract": 2,
                    "fractional_kelly": .25, "max_risk_allocation": .03,
                    "max_premium_allocation": .08, "max_contracts": 2,
                    "max_portfolio_premium_allocation": .20,
                    "max_portfolio_risk_allocation": .09,
                    "max_open_positions": 3,
                    "max_positions_per_underlying": 1,
                    "min_oos_observation_days": 252, "min_oos_completed_trades": 100,
                    "min_oos_independent_events": 100,
                    "evidence": {"approved": True, "out_of_sample": True,
                                 "strategy_version": "0.6.0", "study_path": "study.json",
                                 "study_sha256": digest, "sample_end": "2026-09-08",
                                 "observation_days": 300, "completed_trades": 120,
                                 "independent_events": 110},
                },
            }
            result = resolve_sizing(
                payload, equity=2500, entry_price=1, target_price=2.5,
                stop_price=.45, multiplier=100, strategy_version="0.6.0",
                local_iv_observations=60, min_local_iv_observations=60,
                as_of=date(2026, 9, 9), evidence_base_dir=tmp,
            )
        self.assertEqual(result.mode, "VALIDATED_KELLY")
        self.assertTrue(result.validated_ready)
        self.assertEqual(result.blockers, ())

    def test_auto_sizing_rejects_a_changed_evidence_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "study.json"
            report.write_text('{"study":"approved"}', encoding="utf-8")
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            report.write_text('{"study":"changed"}', encoding="utf-8")
            payload = {
                "mode": "AUTO",
                "cold_start": {"fees_per_contract": 2,
                               "max_risk_allocation": .025,
                               "max_premium_allocation": .05,
                               "max_contracts": 1,
                               "max_portfolio_premium_allocation": .05,
                               "max_portfolio_risk_allocation": .025,
                               "max_open_positions": 1,
                               "max_positions_per_underlying": 1},
                "validated_kelly": {
                    "win_probability_lower_bound": .55,
                    "fees_per_contract": 2, "fractional_kelly": .25,
                    "max_risk_allocation": .03,
                    "max_premium_allocation": .08, "max_contracts": 2,
                    "max_portfolio_premium_allocation": .20,
                    "max_portfolio_risk_allocation": .09,
                    "max_open_positions": 3,
                    "max_positions_per_underlying": 1,
                    "min_oos_observation_days": 60,
                    "min_oos_completed_trades": 30,
                    "evidence": {
                        "approved": True, "out_of_sample": True,
                        "strategy_version": "0.6.0",
                        "study_path": "study.json",
                        "study_sha256": digest,
                        "sample_end": "2026-09-08",
                        "observation_days": 100, "completed_trades": 40,
                    },
                },
            }
            result = resolve_sizing(
                payload, equity=2500, entry_price=1, target_price=2.5,
                stop_price=.45, multiplier=100,
                strategy_version="0.6.0", local_iv_observations=60,
                min_local_iv_observations=60, as_of=date(2026, 9, 9),
                evidence_base_dir=tmp,
            )
        self.assertEqual(result.mode, "COLD_START_FIXED_RISK")
        self.assertIn("EVIDENCE_HASH_MISMATCH", result.blockers)


class BrokerTests(unittest.TestCase):
    class SDK:
        RET_OK = 0
        TrdEnv = SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL")
        TrdSide = SimpleNamespace(BUY="BUY", SELL="SELL")
        OrderType = SimpleNamespace(NORMAL="NORMAL")
        TimeInForce = SimpleNamespace(DAY="DAY")
        ModifyOrderOp = SimpleNamespace(CANCEL="CANCEL", NORMAL="NORMAL")
        Currency = SimpleNamespace(USD="USD")
        TrdMarket = SimpleNamespace(US="US")
        SecurityFirm = SimpleNamespace(NONE="NONE")

    class Context:
        def __init__(self, environment="SIMULATE", orders=None):
            self.environment = environment
            self._orders = orders or []
            self.place_calls = 0
            self.unlock_calls = 0
            self.cancel_calls = 0
            self.modify_calls = 0

        def get_acc_list(self):
            return 0, [{"acc_id": 123, "trd_env": self.environment,
                        "sim_acc_type": "STOCK_AND_OPTION" if self.environment == "SIMULATE" else "N/A"}]

        def accinfo_query(self, **_kwargs):
            return 0, [{"usd_net_cash_power": 4000, "us_cash": 2500,
                        "usd_assets": 3000, "total_assets": 10000,
                        "risk_status": "LEVEL1", "is_pdt": False}]

        def position_list_query(self, **_kwargs):
            return 0, [{"code": "US.AAPL261218C00200000", "qty": 1, "can_sell_qty": 1}]

        def order_list_query(self, **_kwargs):
            return 0, self._orders

        def history_order_list_query(self, **_kwargs):
            return 0, self._orders

        def unlock_trade(self, **_kwargs):
            self.unlock_calls += 1
            return 0, "ok"

        def place_order(self, **kwargs):
            self.place_calls += 1
            return 0, [{"order_id": "B1", "order_status": "SUBMITTING",
                        "remark": kwargs["remark"], "code": kwargs["code"]}]

        def modify_order(self, **kwargs):
            if kwargs["modify_order_op"] == "CANCEL":
                self.cancel_calls += 1
            else:
                self.modify_calls += 1
            return 0, [{"order_id": kwargs["order_id"],
                        "order_status": "CANCELLING"}]

        def close(self):
            pass

    def setUp(self):
        self.now = datetime(2026, 9, 9, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        self.config = BrokerConfig(strategy_equity_cap_usd=2500)
        self.account = AccountSnapshot(self.now.isoformat(), "SIMULATE", 123,
                                       "USD", "usd_net_cash_power", 4000, 2500,
                                       usd_cash=2500, usd_buying_power=4000)
        self.option = LiveQuote("US.AAPL261218C00200000", self.now, self.now,
                                1.15, 1.10, 1.20)
        self.underlying = LiveQuote("US.AAPL", self.now, self.now, 190, 189.9, 190.1)
        signal = {"symbol": "AAPL", "status": "BUY_CANDIDATE",
                  "selected_option_expiry": "2026-12-18",
                  "scenario_target_option_price": 2.4,
                  "active_iv_percentile": .5,
                  "iv_gate_source": "moomoo_option_underlying_rank",
                  "sizing_mode": "VALIDATED_KELLY", "target_spot": 205,
                  "target_source": "PROFILE_HVN", "selected_option_iv": .6,
                  "max_holding_days": 30, "atr": 5}
        self.intent = OrderIntent(
            "LV-20260909-ENTRY-test", "US.AAPL261218C00200000", "BUY", 1,
            1.20, "ENTRY", underlying_code="US.AAPL", reference_spot=190,
            reference_option_ask=1.2, entry_atr=5, hard_stop_spot=183.75,
            option_stop_price=.55, signal_date="2026-09-08",
            metadata={"signal": signal},
        )

    def test_account_snapshot_uses_usd_field_and_strategy_cap(self):
        result = normalize_account_snapshot(
            {"acc_id": 123}, {"usd_net_cash_power": 4000, "total_assets": 10000},
            self.config, self.now,
        )
        self.assertEqual(result.raw_equity, 4000)
        self.assertEqual(result.strategy_equity, 2500)
        self.assertEqual(result.equity_field, "usd_net_cash_power")
        self.assertEqual(result.requested_equity_field, "usd_net_cash_power")
        self.assertEqual(result.equity_fallback_reason, "")

    def test_simulated_account_falls_back_to_explicit_usd_cash(self):
        result = normalize_account_snapshot(
            {"acc_id": 123},
            {"usd_net_cash_power": "N/A", "us_cash": 1_000_000,
             "power": 2_000_000, "total_assets": 1_000_000},
            self.config, self.now,
        )
        self.assertEqual(result.raw_equity, 1_000_000)
        self.assertEqual(result.strategy_equity, 2500)
        self.assertEqual(result.equity_field, "us_cash")
        self.assertEqual(result.requested_equity_field, "usd_net_cash_power")
        self.assertEqual(
            result.equity_fallback_reason,
            "SIMULATE_NET_CASH_POWER_UNAVAILABLE",
        )
        self.assertIsNone(result.usd_buying_power)

    def test_real_account_does_not_fallback_from_net_cash_power(self):
        real_config = BrokerConfig(environment="REAL", strategy_equity_cap_usd=2500)
        with self.assertRaisesRegex(ValueError, "usd_net_cash_power"):
            normalize_account_snapshot(
                {"acc_id": 123},
                {"usd_net_cash_power": "N/A", "us_cash": 1_000_000},
                real_config, self.now,
            )

    def test_live_entry_checks_account_cap_and_spread(self):
        validate_live_order(self.intent, self.account, self.option, self.now,
                            self.config, self.underlying)
        expensive = OrderIntent(**{**self.intent.__dict__, "limit_price": 2.1})
        with self.assertRaisesRegex(RuntimeError, "premium"):
            validate_live_order(expensive, self.account, self.option, self.now,
                                self.config, self.underlying)

    def test_broker_independently_enforces_cold_start_caps(self):
        signal = dict(self.intent.metadata["signal"])
        signal["sizing_mode"] = "COLD_START_FIXED_RISK"
        signal["status"] = "PILOT_CANDIDATE"
        one = OrderIntent(**{**self.intent.__dict__, "limit_price": 1.0,
                             "option_stop_price": .45,
                             "metadata": {"signal": signal}})
        validate_live_order(one, self.account, self.option, self.now,
                            self.config, self.underlying)
        two = OrderIntent(**{**one.__dict__, "quantity": 2})
        with self.assertRaisesRegex(RuntimeError, "contract cap"):
            validate_live_order(two, self.account, self.option, self.now,
                                self.config, self.underlying)

    def test_live_order_separates_planned_stop_and_full_premium_caps(self):
        strict_risk = BrokerConfig(
            strategy_equity_cap_usd=2500, max_entry_risk_fraction=.01,
            max_entry_premium_fraction=.10,
        )
        with self.assertRaisesRegex(RuntimeError, "planned loss"):
            validate_live_order(self.intent, self.account, self.option,
                                self.now, strict_risk, self.underlying)

    def test_simulated_order_and_remote_remark_idempotency(self):
        context = self.Context()
        broker = MoomooBroker(self.config, context=context, sdk=self.SDK)
        result = broker.place_order(self.intent, submit=True)
        self.assertTrue(result["submitted"])
        self.assertEqual(context.place_calls, 1)
        duplicate_context = self.Context(orders=[{"remark": self.intent.client_order_id,
                                                  "order_id": "OLD"}])
        duplicate_broker = MoomooBroker(self.config, context=duplicate_context, sdk=self.SDK)
        duplicate = duplicate_broker.place_order(self.intent, submit=True)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate_context.place_calls, 0)

    def test_real_order_requires_both_config_and_confirmation(self):
        real_config = BrokerConfig(environment="REAL", allow_live_orders=True)
        context = self.Context(environment="REAL")
        broker = MoomooBroker(real_config, context=context, sdk=self.SDK)
        with self.assertRaisesRegex(RuntimeError, "confirmation"):
            broker.place_order(self.intent, submit=True)
        self.assertEqual(context.place_calls, 0)

    def test_simulated_cancel_uses_trade_api(self):
        context = self.Context()
        broker = MoomooBroker(self.config, context=context, sdk=self.SDK)
        result = broker.cancel_order("B1", submit=True)
        self.assertTrue(result["cancelled"])
        self.assertEqual(context.cancel_calls, 1)

    def test_simulated_risk_order_price_modification(self):
        context = self.Context()
        broker = MoomooBroker(self.config, context=context, sdk=self.SDK)
        result = broker.modify_order_price("B1", 1, .75, submit=True)
        self.assertTrue(result["modified"])
        self.assertEqual(context.modify_calls, 1)

    def test_reserved_position_is_not_treated_as_sellable(self):
        rows = [{"code": "US.AC", "qty": 1, "can_sell_qty": 0}]
        self.assertEqual(_broker_position_quantity(rows, "US.AC"), 1)
        self.assertEqual(_broker_position_quantity(rows, "US.AC", sellable=True), 0)

    def test_terminal_broker_fill_materializes_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            database = root / "log.sqlite3"
            positions = root / "positions.csv"
            log = TradingLog(database)
            run_id = log.new_run("2026-09-09", "account_sync")
            signal = {
                "symbol": "AAPL", "status": "PILOT_CANDIDATE",
                "as_of": "2026-09-08", "atr": 5,
                "selected_option_expiry": "2026-12-18",
                "scenario_target_option_price": 2.4,
                "active_iv_percentile": .5,
                "iv_gate_source": "moomoo_option_underlying_rank",
                "sizing_mode": "COLD_START_FIXED_RISK", "target_spot": 205,
                "target_source": "PROFILE_HVN", "selected_option_iv": .6,
                "max_holding_days": 30, "reasons": "test thesis",
            }
            intent = {**self.intent.__dict__, "metadata": {
                "signal": signal, "submission_spot": 190,
                "submission_option_iv": .6,
            }}
            result = {"order": {
                "order_id": "B-FILL", "order_status": "FILLED_ALL",
                "dealt_qty": 1, "dealt_avg_price": 1.2,
                "updated_time": "2026-09-09 11:01:00",
            }}
            log.record_broker_order(intent, result, "SIMULATE", 123, run_id)
            first = _materialize_broker_fills(
                log, run_id, "SIMULATE", 123, str(positions), 2.0)
            second = _materialize_broker_fills(
                log, run_id, "SIMULATE", 123, str(positions), 2.0)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            materialized = read_positions(positions)
            self.assertEqual(len(materialized), 1)
            self.assertEqual(materialized[0].contracts, 1)
            self.assertAlmostEqual(materialized[0].risk_per_contract, 69)
            self.assertAlmostEqual(
                materialized[0].capital_at_risk_per_contract, 122)
            self.assertFalse(materialized[0].partial_exit_taken)
            log.close()


class IntradayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        self.config = BrokerConfig()
        self.underlying = LiveQuote("US.A", self.now, self.now, 100, 99.9, 100.1)
        self.option = LiveQuote("US.AC", self.now, self.now, 1.15, 1.10, 1.20)

    def test_entry_requires_next_session_normal_spread_and_no_chase(self):
        signal = {"status": "BUY_CANDIDATE", "selected_option": "US.AC",
                  "symbol": "A", "as_of": "2026-09-08", "current_price": 100,
                  "entry_hard_stop_spot": 95, "atr": 4,
                  "selected_option_ask": 1.2, "position_contracts": 1,
                  "scenario_target_option_price": 2.4,
                  "scenario_stop_option_price": .55}
        ready = evaluate_intraday_entry(signal, self.underlying, self.option,
                                        self.now, self.config,
                                        next_eligible_session=self.now.date())
        self.assertEqual(ready.action, "PLACE_ENTRY")
        wide = LiveQuote("US.AC", self.now, self.now, 1.1, .8, 1.4)
        waiting = evaluate_intraday_entry(signal, self.underlying, wide,
                                          self.now, self.config,
                                          next_eligible_session=self.now.date())
        self.assertEqual(waiting.reason, "ENTRY_SPREAD_TOO_WIDE")

    def test_hard_exit_is_not_blocked_by_wide_spread(self):
        position = Position("A", "US.AC", date(2026, 8, 1), 1.2, 100, 1, 70,
                            date(2026, 12, 18), hard_stop_spot=98,
                            option_stop_price=.5, target_spot=110,
                            target_option_price=2.4)
        stopped = LiveQuote("US.A", self.now, self.now, 97.5, 97.4, 97.6)
        wide = LiveQuote("US.AC", self.now, self.now, .8, .5, 1.2)
        decision = evaluate_intraday_exit(position, stopped, wide,
                                          self.now, self.config)
        self.assertEqual(decision.action, "PLACE_EXIT")
        self.assertEqual(decision.reason, "UNDERLYING_HARD_STOP")
        self.assertTrue(decision.risk_exit)

    def test_pilot_candidate_uses_generic_position_quantity(self):
        signal = {"status": "PILOT_CANDIDATE", "selected_option": "US.AC",
                  "symbol": "A", "as_of": "2026-09-08", "current_price": 100,
                  "entry_hard_stop_spot": 95, "atr": 4,
                  "selected_option_ask": 1.2, "position_contracts": 1,
                  "scenario_target_option_price": 2.4,
                  "scenario_stop_option_price": .55}
        decision = evaluate_intraday_entry(signal, self.underlying, self.option,
                                           self.now, self.config,
                                           next_eligible_session=self.now.date())
        self.assertEqual(decision.action, "PLACE_ENTRY")
        self.assertEqual(decision.quantity, 1)

    def test_entry_is_valid_only_on_the_next_market_session(self):
        signal = {"status": "BUY_CANDIDATE", "selected_option": "US.AC",
                  "symbol": "A", "as_of": "2026-09-04", "current_price": 100,
                  "entry_hard_stop_spot": 95, "atr": 4,
                  "selected_option_ask": 1.2, "position_contracts": 1,
                  "scenario_target_option_price": 2.4,
                  "scenario_stop_option_price": .55}
        monday = self.now.replace(year=2026, month=9, day=7)
        underlying = LiveQuote("US.A", monday, monday, 100, 99.9, 100.1)
        option = LiveQuote("US.AC", monday, monday, 1.15, 1.10, 1.20)
        ready = evaluate_intraday_entry(
            signal, underlying, option, monday, self.config,
            next_eligible_session=date(2026, 9, 7),
        )
        self.assertEqual(ready.action, "PLACE_ENTRY")

        expired = evaluate_intraday_entry(
            signal, self.underlying, self.option, self.now, self.config,
            next_eligible_session=date(2026, 9, 7),
        )
        self.assertEqual(expired.action, "CANCEL")
        self.assertEqual(expired.reason, "SIGNAL_EXPIRED")

    def test_entry_fails_closed_without_market_calendar(self):
        signal = {"status": "BUY_CANDIDATE", "selected_option": "US.AC",
                  "symbol": "A", "as_of": "2026-09-08"}
        decision = evaluate_intraday_entry(
            signal, self.underlying, self.option, self.now, self.config,
        )
        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.reason, "TRADING_CALENDAR_REQUIRED")

    def test_premium_stop_wide_spread_is_risk_alert(self):
        position = Position(
            "A", "US.AC", date(2026, 8, 20), 1.2, 100, 2, 70,
            date(2026, 12, 18), hard_stop_spot=98,
            option_stop_price=.8, target_spot=110,
            target_option_price=2.4, max_hold_days=90,
        )
        wide = LiveQuote("US.AC", self.now, self.now, .7, .6, 1.2)
        decision = evaluate_intraday_exit(
            position, self.underlying, wide, self.now, self.config,
        )
        self.assertEqual(decision.action, "ALERT")
        self.assertEqual(decision.reason, "PREMIUM_STOP_WIDE_SPREAD")
        self.assertTrue(decision.risk_exit)
        self.assertEqual(decision.quantity, 2)

    def test_structure_target_can_reduce_only_once(self):
        reached = LiveQuote("US.A", self.now, self.now, 111, 110.9, 111.1)
        position = Position(
            "A", "US.AC", date(2026, 8, 20), 1.2, 100, 4, 70,
            date(2026, 12, 18), hard_stop_spot=98,
            option_stop_price=.5, target_spot=110,
            target_option_price=2.4, max_hold_days=90,
        )
        first = evaluate_intraday_exit(
            position, reached, self.option, self.now, self.config,
        )
        self.assertEqual(first.reason, "STRUCTURE_TARGET_REACHED")
        self.assertEqual(first.quantity, 2)

        runner = Position(**{**position.__dict__, "contracts": 2,
                             "partial_exit_taken": True})
        second = evaluate_intraday_exit(
            runner, reached, self.option, self.now, self.config,
        )
        self.assertEqual(second.action, "HOLD")
        self.assertEqual(second.reason, "PARTIAL_TARGET_ALREADY_TAKEN")


class StrategyTests(unittest.TestCase):
    def test_tracked_universe_expires_stale_symbols_but_preserves_positions(self):
        old = [{"symbol": "A", "added_on": "2026-07-01", "last_seen": "2026-07-01",
                "active": "true", "screen_change_5d": "-.10"}]
        tracked, candidates = _merge_tracked_universe(old, [], "2026-09-08", 30)
        self.assertEqual(tracked, [])
        self.assertEqual(candidates, [])
        tracked, candidates = _merge_tracked_universe(old, [], "2026-09-08", 30, {"A"})
        self.assertEqual(tracked[0]["symbol"], "A")
        self.assertEqual(float(candidates[0]["screen_change_5d"]), -.10)

    def test_evaluation_does_not_buy_without_compression(self):
        as_of = date(2026, 3, 5)
        chain = [quote(as_of)]
        cfg = Config(min_history_bars=20, require_intraday_profile=False,
                     min_option_open_interest=0, min_option_volume=0)
        e = evaluate(Candidate("A", as_of), bars(list(range(100, 40, -1)) + [39, 38, 37, 37.5]),
                     chain, ShortSnapshot(as_of, 90, 100, .4, .5), None, cfg)
        self.assertIn(e.status, {"WATCH", "NO_TRADE"})
        self.assertFalse(e.hard_gates["stabilization"])

    def test_weak_research_features_are_optional_but_remain_observable(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        cfg = Config(min_history_bars=20, min_option_open_interest=0,
                     min_option_volume=0)
        evaluation = evaluate(
            Candidate("A", as_of), data, [quote(as_of)], None, None, cfg,
        )
        self.assertTrue(evaluation.hard_gates["profile_data"])
        self.assertTrue(evaluation.hard_gates["rebound_geometry"])
        self.assertTrue(evaluation.hard_gates["short_weakening"])
        self.assertFalse(evaluation.metrics["profile_data_observed"])
        self.assertFalse(evaluation.metrics["short_weakening_observed"])
        self.assertFalse(evaluation.metrics["rebound_geometry_required"])

    def test_weak_research_features_can_be_explicitly_promoted_to_gates(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        cfg = Config(
            min_history_bars=20, min_option_open_interest=0,
            min_option_volume=0, require_intraday_profile=True,
            require_rebound_geometry_gate=True,
            require_short_weakening_gate=True,
        )
        evaluation = evaluate(
            Candidate("A", as_of), data, [quote(as_of)], None, None, cfg,
        )
        self.assertFalse(evaluation.hard_gates["profile_data"])
        self.assertFalse(evaluation.hard_gates["rebound_geometry"])
        self.assertFalse(evaluation.hard_gates["short_weakening"])

    def test_uncertain_fundamental_thesis_cannot_pass_entry_gate(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        uncertain = FundamentalSnapshot(
            thesis_status="uncertain", confidence=.95, as_of=as_of,
            needs_review=False,
        )
        evaluation = evaluate(
            Candidate("A", as_of), data, [quote(as_of)], None, uncertain,
            Config(min_history_bars=20, min_option_open_interest=0,
                   min_option_volume=0),
        )
        self.assertTrue(evaluation.hard_gates["thesis_not_broken"])
        self.assertFalse(evaluation.hard_gates["thesis_intact"])
        self.assertFalse(evaluation.hard_gates["fundamental_review"])
        self.assertEqual(evaluation.metrics["fundamental_thesis_status"],
                         "uncertain")

    def test_intact_reviewed_fundamental_thesis_passes_entry_gate(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        intact = FundamentalSnapshot(
            thesis_status="intact", confidence=.95, as_of=as_of,
            needs_review=False,
            event_class="LIQUIDITY_OR_TECHNICAL_DISLOCATION",
            evidence=("source-1: abnormal technical flow",),
            sources=("https://example.com/source-1",),
            source_ids=("source-1",),
            news_packet_sha256="a" * 64,
            point_in_time_collection_ok=True,
        )
        evaluation = evaluate(
            Candidate("A", as_of), data, [quote(as_of)], None, intact,
            Config(min_history_bars=20, min_option_open_interest=0,
                   min_option_volume=0),
        )
        self.assertTrue(evaluation.hard_gates["thesis_intact"])
        self.assertTrue(evaluation.hard_gates["fundamental_review"])
        self.assertTrue(evaluation.hard_gates["fundamental_temporary_dislocation"])
        self.assertTrue(evaluation.hard_gates["fundamental_pit_evidence"])

    def test_intact_label_without_pit_temporary_dislocation_evidence_fails(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        ungrounded = FundamentalSnapshot(
            thesis_status="intact", confidence=.95, as_of=as_of,
            needs_review=False,
        )
        evaluation = evaluate(
            Candidate("A", as_of), data, [quote(as_of)], None, ungrounded,
            Config(min_history_bars=20, min_option_open_interest=0,
                   min_option_volume=0),
        )
        self.assertTrue(evaluation.hard_gates["thesis_intact"])
        self.assertFalse(evaluation.hard_gates["fundamental_temporary_dislocation"])
        self.assertFalse(evaluation.hard_gates["fundamental_pit_evidence"])
        self.assertFalse(evaluation.hard_gates["fundamental_review"])

    def test_fresh_short_deterioration_is_a_veto_not_a_weakening_requirement(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        worsening = ShortSnapshot(
            as_of, short_interest=110, short_interest_5d_ago=100,
            short_interest_date=as_of,
        )
        evaluation = evaluate(
            Candidate("A", as_of), data, [quote(as_of)], worsening, None,
            Config(min_history_bars=20, min_option_open_interest=0,
                   min_option_volume=0),
        )
        self.assertTrue(evaluation.hard_gates["short_weakening"])
        self.assertFalse(evaluation.hard_gates["short_not_worsening"])
        self.assertTrue(evaluation.metrics["short_deterioration_observed"])

    def test_residual_shock_is_diagnostic_until_explicitly_required(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        candidate = Candidate("A", as_of, screener_values={
            "screen_change_5d": -.15,
            "screen_market_change_5d": -.03,
        })
        diagnostic = evaluate(
            candidate, data, [quote(as_of)], None, None,
            Config(min_history_bars=20, min_option_open_interest=0,
                   min_option_volume=0, max_market_residual_5d=-.13),
        )
        self.assertTrue(diagnostic.hard_gates["market_residual_shock"])
        self.assertFalse(diagnostic.metrics["market_residual_shock_observed"])

        required = evaluate(
            candidate, data, [quote(as_of)], None, None,
            Config(min_history_bars=20, min_option_open_interest=0,
                   min_option_volume=0, require_market_residual_shock=True,
                   max_market_residual_5d=-.13),
        )
        self.assertFalse(required.hard_gates["market_residual_shock"])

    def test_unsigned_gamma_concentration_does_not_set_target_by_default(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        chain = [quote(as_of), OptionQuote(
            "A261218C00101000", as_of + timedelta(days=90), 101, "C",
            2.4, 2.6, 2.5, .60, .40, 1.0, 100000, 100,
            quote_time=datetime.combine(as_of, datetime.min.time()),
        )]
        evaluation = evaluate(
            Candidate("A", as_of), data, chain, None, None,
            Config(min_history_bars=20, min_option_open_interest=0,
                   min_option_volume=0),
        )
        self.assertNotEqual(evaluation.metrics["target_source"],
                            "GAMMA_CONCENTRATION_PROXY")
        self.assertFalse(evaluation.metrics["unsigned_gamma_target_enabled"])

    def test_post_panic_sequence_waits_two_complete_sessions(self):
        start = date(2026, 1, 1)
        prices = [100.0] * 60 + [100 - i * .5 for i in range(1, 20)] + [70.0]
        data = []
        for i, price in enumerate(prices):
            opening = 85 if i == len(prices) - 1 else price
            data.append(Bar(
                start + timedelta(days=i), opening, max(opening, price) * 1.01,
                min(opening, price) * .99, price,
                2000 if i == len(prices) - 1 else 100 + (i % 3) * 10,
            ))
        shock_day = data[-1].day
        cfg = Config(min_history_bars=20, require_intraday_profile=False,
                     min_option_open_interest=0, min_option_volume=0)
        on_shock = evaluate(
            Candidate("A", shock_day), data, [quote(shock_day)], None, None, cfg)
        self.assertTrue(on_shock.hard_gates["panic_confirmed"])
        self.assertFalse(on_shock.metrics["post_panic_sequence_ready"])
        self.assertFalse(on_shock.metrics["compressed"])

        data.extend([
            Bar(shock_day + timedelta(days=1), 71, 72, 70.5, 71.5, 500),
            Bar(shock_day + timedelta(days=2), 72, 73, 71.5, 72.5, 400),
        ])
        after_wait = evaluate(
            Candidate("A", data[-1].day), data, [quote(data[-1].day)],
            None, None, cfg)
        self.assertTrue(after_wait.hard_gates["panic_confirmed"])
        self.assertEqual(after_wait.metrics["sessions_since_recent_shock"], 2)
        self.assertTrue(after_wait.metrics["post_panic_sequence_ready"])

    def test_reversal_cannot_pass_after_making_a_new_intraday_low(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 77, start=date(2025, 12, 15))
        data[-3] = Bar(data[-3].day, 90, 91, 89, 90, 100)
        data[-2] = Bar(data[-2].day, 89, 90, 88, 89, 100)
        data[-1] = Bar(as_of, 88, 91, 87, 90.5, 100)
        cfg = Config(min_history_bars=20, require_intraday_profile=False,
                     min_option_open_interest=0, min_option_volume=0)
        evaluation = evaluate(Candidate("A", as_of), data, [quote(as_of)], None, None, cfg)
        self.assertFalse(evaluation.metrics["not_new_low"])

    def test_cold_start_iv_gate_uses_only_fresh_moomoo_percentile(self):
        as_of = date(2026, 3, 5)
        data = bars([100.0] * 80, start=date(2025, 12, 16))
        cfg = Config(min_history_bars=20, require_intraday_profile=False,
                     min_option_open_interest=0, min_option_volume=0,
                     require_iv_regime_gate=True)
        regime = OptionRegimeSnapshot("A", as_of, iv=.5, iv_percentile=.7)
        sizing = FixedRiskInput(2500, 2.6, 5.0, 1.1, max_risk_allocation=.1,
                                max_premium_allocation=.2)
        evaluation = evaluate(
            Candidate("A", as_of), data, [quote(as_of)], None, None, cfg,
            sizing=sizing, sizing_mode="COLD_START_FIXED_RISK",
            option_regime=regime,
        )
        self.assertTrue(evaluation.hard_gates["iv_regime"])
        self.assertEqual(evaluation.metrics["iv_gate_source"],
                         "moomoo_option_underlying_rank")
        stale = evaluate(
            Candidate("A", as_of), data, [quote(as_of)], None, None, cfg,
            sizing=sizing, sizing_mode="COLD_START_FIXED_RISK",
            option_regime=OptionRegimeSnapshot("A", as_of - timedelta(days=1),
                                               iv=.5, iv_percentile=.7),
        )
        self.assertFalse(stale.hard_gates["iv_regime"])


class ExitTests(unittest.TestCase):
    def setUp(self):
        self.as_of = date(2026, 9, 4)
        self.bars = bars([100] * 90, start=date(2026, 6, 7))
        self.evaluation = Evaluation("A", self.as_of, "WATCH", {"thesis_not_broken": True},
                                     {"valuation_regime_break": False, "worsening": False})
        self.position = Position("A", "A261218C00100000", date(2026, 8, 20), 2.5, 100, 1, 150,
                                 date(2026, 12, 18), entry_atr=2, hard_stop_spot=97.5,
                                 option_stop_price=1.1, target_spot=110, target_option_price=5,
                                 target_source="PROFILE_HVN", entry_iv=.60, max_hold_days=30,
                                 max_option_price=3.0)

    def test_missing_option_bid_blocks_decision(self):
        result = evaluate_exit(self.position, self.bars, self.evaluation, as_of=self.as_of)
        self.assertEqual(result["action"], "DATA_NEEDED")

    def test_underlying_stop_is_frozen_at_entry(self):
        stopped_bars = self.bars[:-1] + [Bar(self.as_of, 98, 120, 97.6, 98, 1000)]
        result = evaluate_exit(self.position, stopped_bars, self.evaluation,
                               quote(self.as_of), as_of=self.as_of)
        self.assertNotEqual(result["reason"], "UNDERLYING_HARD_STOP")
        self.assertEqual(result["hard_stop_spot"], 97.5)

    def test_option_premium_stop(self):
        result = evaluate_exit(self.position, self.bars, self.evaluation,
                               quote(self.as_of, bid=1.0, ask=1.05), as_of=self.as_of)
        self.assertEqual(result["action"], "EXIT")
        self.assertEqual(result["reason"], "OPTION_PREMIUM_STOP")
        self.assertTrue(result["risk_exit"])

    def test_wide_spread_premium_stop_is_an_alert_not_hold(self):
        result = evaluate_exit(
            self.position, self.bars, self.evaluation,
            quote(self.as_of, bid=1.0, ask=2.0), as_of=self.as_of,
        )
        self.assertEqual(result["action"], "ALERT")
        self.assertEqual(result["reason"], "OPTION_PREMIUM_STOP_WIDE_SPREAD")
        self.assertTrue(result["risk_exit"])
        self.assertEqual(result["suggested_quantity"], 1)

    def test_underlying_intraday_low_triggers_exit_without_assumed_fill(self):
        stopped = self.bars[:-1] + [Bar(self.as_of, 98, 100, 97.0, 99, 1000)]
        result = evaluate_exit(self.position, stopped, self.evaluation,
                               quote(self.as_of), as_of=self.as_of)
        self.assertEqual(result["reason"], "UNDERLYING_HARD_STOP")
        self.assertFalse(result["execution_price_guaranteed"])

    def test_underlying_stop_is_not_hidden_by_missing_option_quote(self):
        stopped = self.bars[:-1] + [Bar(self.as_of, 98, 100, 97.0, 99, 1000)]
        result = evaluate_exit(self.position, stopped, self.evaluation, as_of=self.as_of)
        self.assertEqual(result["action"], "EXIT")
        self.assertEqual(result["reason"], "UNDERLYING_HARD_STOP")
        self.assertEqual(result["suggested_order"], "MANUAL_QUOTE_REQUIRED")
        self.assertIn("CURRENT_OPTION_BID", result["missing_inputs"])

    def test_option_pnl_uses_contract_multiplier(self):
        adjusted = Position(**{**self.position.__dict__, "multiplier": 50})
        result = evaluate_exit(adjusted, self.bars, self.evaluation,
                               quote(self.as_of, bid=4.0, ask=4.1), as_of=self.as_of)
        self.assertAlmostEqual(result["r_multiple"], .5)

    def test_structure_must_be_reached(self):
        result = evaluate_exit(self.position, self.bars, self.evaluation,
                               quote(self.as_of, bid=4.0, ask=4.1), as_of=self.as_of)
        self.assertNotEqual(result["reason"], "STRUCTURE_TARGET_REACHED")

    def test_single_contract_exits_instead_of_fake_trim(self):
        reached = self.bars[:-1] + [Bar(self.as_of, 110, 111, 109, 110, 1000)]
        result = evaluate_exit(self.position, reached, self.evaluation,
                               quote(self.as_of, bid=5.1, ask=5.2), as_of=self.as_of)
        self.assertEqual(result["action"], "EXIT")
        self.assertEqual(result["suggested_quantity"], 1)

    def test_structure_target_reduces_multi_contract_position_only_once(self):
        reached = self.bars[:-1] + [Bar(self.as_of, 110, 111, 109, 110, 1000)]
        multi = Position(**{**self.position.__dict__, "contracts": 4,
                            "target_option_price": 6.0})
        first = evaluate_exit(
            multi, reached, self.evaluation,
            quote(self.as_of, bid=4.0, ask=4.1), as_of=self.as_of,
        )
        self.assertEqual(first["action"], "REDUCE")
        self.assertEqual(first["suggested_quantity"], 2)

        runner = Position(**{**multi.__dict__, "contracts": 2,
                             "partial_exit_taken": True})
        second = evaluate_exit(
            runner, reached, self.evaluation,
            quote(self.as_of, bid=4.0, ask=4.1), as_of=self.as_of,
        )
        self.assertEqual(second["action"], "HOLD")
        self.assertEqual(second["reason"], "NO_EXIT_TRIGGER")

    def test_iv_crush_exit(self):
        result = evaluate_exit(self.position, self.bars, self.evaluation,
                               quote(self.as_of, bid=2.2, ask=2.3, iv=.40), as_of=self.as_of)
        self.assertEqual(result["reason"], "IV_CRUSH")


class JournalTests(unittest.TestCase):
    def test_journal_keeps_features_trades_and_exit_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = TradingLog(Path(tmp) / "journal.sqlite3")
            run_id = log.new_run("2026-09-04", config_sha256="abc", app_version="0.4.0")
            candidate = Candidate("A", date(2026, 9, 4), "panic")
            log.record_candidate(run_id, candidate)
            evaluation = Evaluation("A", date(2026, 9, 4), "WATCH", {"price_flow": True}, {"iv": .8})
            log.record_evaluation(run_id, evaluation)
            log.record_sizing_mode(run_id, "A", "2026-09-04",
                                   "COLD_START_FIXED_RISK", False,
                                   ["LOCAL_IV_HISTORY_INCOMPLETE"], {})
            position = Position("A", "A261218C00100000", date(2026, 9, 4), 2, 100, 1, 100, date(2026, 12, 18))
            log.record_trade("OPEN", position, "2026-09-04", 1, 100, 2, run_id=run_id, features={"iv": .8})
            log.record_exit_decision(run_id, "2026-09-04", {"symbol": "A", "option_symbol": position.option_symbol,
                                                            "action": "HOLD", "reason": "NO_EXIT_TRIGGER"})
            log.finish_run(run_id)
            self.assertEqual(log.db.execute("select count(*) from feature_snapshots").fetchone()[0], 1)
            self.assertEqual(log.db.execute("select count(*) from trades").fetchone()[0], 1)
            self.assertEqual(log.db.execute("select count(*) from exit_decisions").fetchone()[0], 1)
            self.assertEqual(log.db.execute("select count(*) from sizing_mode_decisions").fetchone()[0], 1)
            stored_multiplier, position_json = log.db.execute(
                "select multiplier, position_json from trades").fetchone()
            self.assertEqual(stored_multiplier, 100)
            self.assertIn("hard_stop_spot", position_json)
            log.close()

    def test_event_is_durable_without_finishing_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.sqlite3"
            log = TradingLog(path)
            log.event("ERROR", "COMMAND_FAILED", "synthetic")
            log.close()
            verify = sqlite3.connect(path)
            self.assertEqual(verify.execute("select count(*) from events").fetchone()[0], 1)
            verify.close()

    def test_record_trade_closes_position_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trade_path = root / "trade.json"
            positions_path = root / "positions.csv"
            db_path = root / "journal.sqlite3"
            opened = {
                "action": "OPEN", "trade_date": "2026-09-04", "symbol": "A",
                "option_symbol": "A261218C00100000", "entry_date": "2026-09-04",
                "entry_price": 2.5, "entry_spot": 100, "contracts": 1, "quantity": 1,
                "risk_per_contract": 150, "expiry": "2026-12-18", "spot_price": 100,
                "option_price": 2.5, "entry_atr": 2, "hard_stop_spot": 97.5,
                "option_stop_price": 1.0, "target_spot": 110, "target_option_price": 5,
                "target_source": "PROFILE_HVN", "entry_iv": .6, "multiplier": 100,
                "entry_iv_percentile": .5,
            }
            trade_path.write_text(json.dumps(opened), encoding="utf-8")
            args = SimpleNamespace(trade_json=str(trade_path), log_db=str(db_path),
                                   positions_out=str(positions_path))
            record_trade(args)
            closed = {"action": "CLOSE", "trade_date": "2026-09-10",
                      "option_symbol": opened["option_symbol"], "quantity": 1,
                      "spot_price": 108, "option_price": 4.8, "fees": 2}
            trade_path.write_text(json.dumps(closed), encoding="utf-8")
            record_trade(args)
            materialized = read_positions(positions_path)
            self.assertEqual(materialized[-1].status, "CLOSED")
            self.assertEqual(materialized[-1].contracts, 0)
            verify_log = TradingLog(db_path)
            exit_snapshot = verify_log.db.execute(
                "select position_json from trades where action='CLOSE'").fetchone()[0]
            self.assertIn('"status": "CLOSED"', exit_snapshot)
            verify_log.close()

    def test_reduce_fill_sets_partial_state_and_second_reduce_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trade_path = root / "trade.json"
            positions_path = root / "positions.csv"
            db_path = root / "journal.sqlite3"
            opened = {
                "action": "OPEN", "trade_date": "2026-09-04", "symbol": "A",
                "option_symbol": "A261218C00100000", "entry_date": "2026-09-04",
                "entry_price": 2.5, "entry_spot": 100, "contracts": 4,
                "quantity": 4, "risk_per_contract": 150,
                "capital_at_risk_per_contract": 252,
                "expiry": "2026-12-18", "spot_price": 100,
                "option_price": 2.5, "fees": 8, "entry_atr": 2,
                "hard_stop_spot": 97.5, "option_stop_price": 1.0,
                "target_spot": 110, "target_option_price": 5,
                "target_source": "PROFILE_HVN", "entry_iv": .6,
                "entry_iv_percentile": .5, "multiplier": 100,
            }
            args = SimpleNamespace(trade_json=str(trade_path), log_db=str(db_path),
                                   positions_out=str(positions_path))
            trade_path.write_text(json.dumps(opened), encoding="utf-8")
            record_trade(args)
            reduced = {
                "action": "REDUCE", "trade_date": "2026-09-10",
                "option_symbol": opened["option_symbol"], "quantity": 2,
                "spot_price": 110, "option_price": 4.0, "fees": 4,
            }
            trade_path.write_text(json.dumps(reduced), encoding="utf-8")
            record_trade(args)
            runner = read_positions(positions_path)[0]
            self.assertEqual(runner.contracts, 2)
            self.assertTrue(runner.partial_exit_taken)
            self.assertEqual(runner.capital_at_risk_per_contract, 252)

            second_reduce = {**reduced, "quantity": 1,
                             "trade_date": "2026-09-11"}
            trade_path.write_text(json.dumps(second_reduce), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "only one partial"):
                record_trade(args)

    def test_record_trade_rejects_inconsistent_fill_and_understated_risk(self):
        base = {
            "action": "OPEN", "trade_date": "2026-09-04", "symbol": "A",
            "option_symbol": "A261218C00100000", "entry_date": "2026-09-04",
            "entry_price": 2.5, "entry_spot": 100, "contracts": 1, "quantity": 1,
            "risk_per_contract": 150, "expiry": "2026-12-18", "spot_price": 100,
            "option_price": 2.5, "entry_atr": 2, "hard_stop_spot": 97.5,
            "option_stop_price": 1.0, "target_spot": 110, "target_option_price": 5,
            "target_source": "PROFILE_HVN", "entry_iv": .6,
            "entry_iv_percentile": .5, "multiplier": 100,
        }
        for change in ({"entry_price": 2.4}, {"risk_per_contract": 149},
                       {"capital_at_risk_per_contract": 249}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                payload = {**base, **change}
                trade_path = root / "trade.json"
                trade_path.write_text(json.dumps(payload), encoding="utf-8")
                args = SimpleNamespace(trade_json=str(trade_path),
                                       log_db=str(root / "journal.sqlite3"),
                                       positions_out=str(root / "positions.csv"))
                with self.assertRaises(ValueError):
                    record_trade(args)

    def test_broker_order_id_is_idempotency_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = TradingLog(Path(tmp) / "journal.sqlite3")
            position = Position("A", "A261218C00100000", date(2026, 9, 4), 2, 100,
                                1, 100, date(2026, 12, 18))
            log.record_trade("OPEN", position, "2026-09-04", 1, 100, 2,
                             broker_order_id="ORDER-1")
            with self.assertRaises(sqlite3.IntegrityError):
                log.record_trade("OPEN", position, "2026-09-04", 1, 100, 2,
                                 broker_order_id="ORDER-1")
            log.close()


if __name__ == "__main__":
    unittest.main()
