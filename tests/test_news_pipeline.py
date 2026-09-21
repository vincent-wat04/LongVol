from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from longvol.cli import (healthcheck, news_sync, research_capabilities, scan,
                         _fundamental_fail_closed)
from longvol.io import merge_fundamental, read_fundamentals, write_rows
from longvol.models import Bar, Config, Evaluation, OptionQuote
from longvol.news import (NewsArticle, append_news_archive, evidence_packet,
                          provider_configuration, read_news_archive)
from longvol.option_snapshots import archive_option_snapshot


UTC = timezone.utc


class _Provider:
    name = "fixture"
    gate_eligible = True
    broad_news_coverage = True

    def __init__(self, article: NewsArticle):
        self.article = article

    def fetch(self, symbol, start, end):
        return [self.article]


class NewsPipelineTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = datetime(2026, 9, 10, tzinfo=UTC)
        self.start = self.cutoff - timedelta(days=7)
        self.article = NewsArticle(
            provider="fixture", source_id="n-1", source_name="Issuer",
            source_tier="PRIMARY_ISSUER", title="Dated update",
            url="https://example.com/n-1",
            published_at=self.cutoff - timedelta(days=1),
            first_seen_at=self.cutoff - timedelta(hours=2),
            ingested_at=self.cutoff - timedelta(hours=2),
            available_at=self.cutoff - timedelta(hours=2),
            symbols=("TST",), summary="Point-in-time summary.",
        )

    def test_archive_is_append_only_and_deduplicates_exact_revision(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "archive" / "TST.jsonl"
            self.assertEqual(append_news_archive(path, [self.article]), 1)
            original = path.read_bytes()
            self.assertEqual(append_news_archive(path, [self.article]), 0)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(read_news_archive(path), [self.article])

    def test_packet_hash_changes_when_gate_coverage_or_local_context_changes(self):
        good = [{"provider": "fixture", "ok": True, "gate_eligible": True,
                 "broad_news_coverage": True}]
        failed = [{"provider": "fixture", "ok": False, "gate_eligible": True,
                   "broad_news_coverage": True}]
        first = evidence_packet("TST", self.start, self.cutoff, [self.article], good)
        second = evidence_packet("TST", self.start, self.cutoff, [self.article], failed)
        third = evidence_packet(
            "TST", self.start, self.cutoff, [self.article], good, "operator note",
        )
        self.assertNotEqual(first["packet_sha256"], second["packet_sha256"])
        self.assertNotEqual(first["packet_sha256"], third["packet_sha256"])

    def test_news_sync_persists_archive_and_dated_packet(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                symbols="TST", start="2026-09-01", as_of="2026-09-09",
                state_dir=str(root / "state"), providers="fixture",
                local_packet_dir=str(root / "local"),
            )
            (root / "local").mkdir()
            (root / "local" / "TST.txt").write_text("manual context", encoding="utf-8")
            provider = _Provider(self.article)
            configuration = {
                "selection": "fixture", "providers": [],
                "all_requested_configured": True,
                "broad_gate_provider_configured": True,
                "network_probe_run": False,
            }
            with patch("longvol.cli._configured_news_sources",
                       return_value=([provider], [], configuration)):
                with redirect_stdout(io.StringIO()):
                    news_sync(args)
                    news_sync(args)
            archive = root / "state" / "news" / "archive" / "TST.jsonl"
            packet_path = root / "state" / "news" / "packets" / "2026-09-09" / "TST.json"
            self.assertEqual(len(read_news_archive(archive)), 1)
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertEqual(packet["local_context"], "manual context")
            self.assertEqual(packet["article_count"], 1)

    def test_fundamental_metadata_round_trip_and_fail_closed_default(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fundamentals.csv"
            packet = {"packet_sha256": "abc123", "articles": []}
            result = _fundamental_fail_closed("TST", "2026-09-09", packet, "no broad feed")
            result.update({
                "event_class": "LIQUIDITY_OR_TECHNICAL_DISLOCATION",
                "selloff_explanation": "flow pressure",
                "material_news_found": True,
                "source_ids": ["source-1"],
                "sources": ["https://example.com/source-1"],
                "point_in_time_collection_ok": True,
                "retrieval_mode": "POINT_IN_TIME_PACKET",
                "request_id": "request-1",
            })
            merge_fundamental(path, "TST", result, "2026-09-09")
            snapshot = read_fundamentals(path)["TST"]
            self.assertEqual(snapshot.event_class, result["event_class"])
            self.assertEqual(snapshot.source_ids, ("source-1",))
            self.assertEqual(snapshot.news_packet_sha256, "abc123")
            self.assertTrue(snapshot.point_in_time_collection_ok)
            self.assertEqual(snapshot.research_request_id, "request-1")

    def test_healthcheck_reports_configuration_without_any_probe(self):
        args = SimpleNamespace(
            data_dir="data", state_dir="state", opend=False, broker=False,
            research=True, news_providers="none", config="config/strategy.json",
            sizing="config/sizing.json", trading_config="config/trading.json",
            host="127.0.0.1", port=11111,
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            with patch("longvol.fundamental_research.FundamentalResearchClient.probe_native_web_search",
                       side_effect=AssertionError("healthcheck must not probe")):
                output = io.StringIO()
                with redirect_stdout(output):
                    healthcheck(args)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["checks"]["research_configuration"]["capability_probe_run"])
        self.assertFalse(payload["checks"]["news_provider_configuration"]["network_probe_run"])

    def test_delayed_news_credentials_do_not_silently_become_gate_evidence(self):
        credentials = {
            "APCA_API_KEY_ID": "configured",
            "APCA_API_SECRET_KEY": "configured",
            "ALPACA_NEWS_REALTIME_ENTITLED": "false",
        }
        with patch.dict(os.environ, credentials, clear=True):
            delayed = provider_configuration("alpaca")
        self.assertFalse(delayed["providers"][0]["gate_eligible"])
        self.assertFalse(delayed["broad_gate_provider_configured"])
        credentials["ALPACA_NEWS_REALTIME_ENTITLED"] = "true"
        with patch.dict(os.environ, credentials, clear=True):
            realtime = provider_configuration("alpaca")
        self.assertTrue(realtime["providers"][0]["gate_eligible"])
        self.assertTrue(realtime["broad_gate_provider_configured"])

    def test_research_capability_probe_requires_explicit_billable_flag(self):
        with self.assertRaisesRegex(ValueError, "billable-probe"):
            research_capabilities(SimpleNamespace(billable_probe=False, model=None))
        client = Mock()
        client.probe_native_web_search.return_value = {"ok": False}
        output = io.StringIO()
        with patch("longvol.fundamental_research.FundamentalResearchClient",
                   return_value=client):
            with redirect_stdout(output):
                research_capabilities(SimpleNamespace(billable_probe=True, model="fixture"))
        client.probe_native_web_search.assert_called_once_with()
        self.assertTrue(json.loads(output.getvalue())["billable_probe"])

    def test_scan_loads_shock_day_chain_and_uses_pre_shock_iv(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            shock_day = date(2026, 2, 12)
            entry_day = shock_day + timedelta(days=2)
            baseline_start = shock_day - timedelta(days=42)
            bars = [
                Bar(baseline_start + timedelta(days=index), 100, 101, 99, 100,
                    100 + index % 3 * 10)
                for index in range(42)
            ]
            bars.extend([
                Bar(shock_day, 80, 90, 68, 70, 2000),
                Bar(shock_day + timedelta(days=1), 70, 72, 69, 71, 400),
                Bar(entry_day, 71, 73, 70, 72, 300),
            ])
            write_rows(root / "candidates.csv", [{"symbol": "TST", "as_of": entry_day,
                                                   "screener_reason": "test"}])
            write_rows(root / "bars" / "TST.csv", [{
                "day": bar.day, "open": bar.open, "high": bar.high, "low": bar.low,
                "close": bar.close, "volume": bar.volume,
            } for bar in bars])
            option = OptionQuote(
                "TST-C", shock_day + timedelta(days=90), 75, "C", 1, 1.1, 1.05,
                .7, .4, .02, 1000, 100,
                quote_time=datetime.combine(shock_day, datetime.min.time()),
            )
            archive_option_snapshot(root, "TST", shock_day, [option])
            write_rows(root / "option_history.csv", [
                {"symbol": "TST", "day": shock_day - timedelta(days=1),
                 "near_atm_iv": .4},
                {"symbol": "TST", "day": shock_day + timedelta(days=1),
                 "near_atm_iv": .8},
            ])
            (root / "sizing.json").write_text("{}", encoding="utf-8")
            observed = []

            def fake_evaluate(candidate, _bars, _options, _short, _fundamental,
                              _config, **kwargs):
                observed.append(kwargs)
                return Evaluation(candidate.symbol, candidate.as_of, "NO_TRADE", {}, {})

            resolution = SimpleNamespace(
                mode="UNAVAILABLE", sizing=None, validated_ready=False,
                blockers=(), evidence={},
            )
            args = SimpleNamespace(
                config="unused.json", candidates=str(root / "candidates.csv"),
                short=None, fundamentals=None, option_history=str(root / "option_history.csv"),
                option_regime=None, sizing=str(root / "sizing.json"), account_state=None,
                positions=None, log_db=None, as_of=entry_day.isoformat(),
                bars_dir=str(root / "bars"), intraday_dir=None,
                options_dir=str(root / "options"), option_snapshot_root=str(root),
                output=str(root / "signals.csv"),
            )
            config = Config(require_position_sizing=False, min_history_bars=20)
            with patch("longvol.cli.load_config", return_value=config), \
                    patch("longvol.cli.resolve_sizing", return_value=resolution), \
                    patch("longvol.cli.evaluate", side_effect=fake_evaluate):
                with redirect_stdout(io.StringIO()):
                    scan(args)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["shock_option_day"], shock_day)
            self.assertEqual(observed[0]["shock_options"], [option])
            self.assertEqual(observed[0]["previous_near_atm_iv"], .4)


if __name__ == "__main__":
    unittest.main()
