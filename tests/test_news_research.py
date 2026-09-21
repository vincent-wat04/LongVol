from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from longvol.fundamental_research import (FundamentalResearchClient,
                                          verify_snapshot_packet)
from longvol.models import FundamentalSnapshot
from longvol.news import NewsArticle, collect_news, evidence_packet, select_point_in_time


UTC = timezone.utc


class _Provider:
    name = "fixture"

    def __init__(self, rows):
        self.rows = rows

    def fetch(self, symbol, start, end):
        return self.rows


class _Responses:
    def __init__(self, result: dict, output=()):
        self.result = result
        self.output = output

    def create(self, **kwargs):
        return SimpleNamespace(
            output_text=json.dumps(self.result), output=self.output,
            _request_id="request-test",
        )


def _research_result(url: str, source_id: str) -> dict:
    return {
        "thesis_status": "intact",
        "event_class": "LIQUIDITY_OR_TECHNICAL_DISLOCATION",
        "selloff_explanation": "Temporary flow pressure.",
        "material_news_found": True,
        "catalyst_present": False,
        "catalyst": "",
        "valuation_regime_break": False,
        "valuation_reason": "No structural break in the supplied evidence.",
        "as_of": "2026-09-09",
        "evidence": [f"{source_id}: dated evidence"],
        "sources": [url],
        "source_ids": [source_id],
        "confidence": 0.8,
        "needs_review": False,
    }


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.end = datetime(2026, 9, 10, tzinfo=UTC)
        self.start = self.end - timedelta(days=7)
        self.article = NewsArticle(
            provider="fixture", source_id="article-1", source_name="Issuer",
            source_tier="PRIMARY_ISSUER", title="Company update",
            url="https://example.com/update", published_at=self.end - timedelta(days=1),
            first_seen_at=self.end - timedelta(hours=1),
            ingested_at=self.end - timedelta(hours=1),
            available_at=self.end - timedelta(hours=1),
            symbols=("TST",), summary="A dated update.",
        )

    def test_collection_enforces_cutoff_and_deduplicates(self):
        later = NewsArticle(
            provider="fixture", source_id="future", source_name="Issuer",
            source_tier="PRIMARY_ISSUER", title="Future update",
            url="https://example.com/future", published_at=self.end + timedelta(seconds=1),
            symbols=("TST",),
        )
        rows, diagnostics = collect_news(
            "TST", self.start, self.end,
            [_Provider([self.article, self.article, later])],
        )
        self.assertEqual([row.source_id for row in rows], ["article-1"])
        self.assertTrue(diagnostics[0]["ok"])
        self.assertEqual(diagnostics[0]["accepted"], 2)

    def test_rest_backfill_first_seen_after_cutoff_is_not_point_in_time(self):
        backfill = NewsArticle(
            provider="fixture", source_id="old", source_name="Wire",
            source_tier="LICENSED_NEWS", title="Old story",
            url="https://example.com/old", published_at=self.end - timedelta(days=2),
            first_seen_at=self.end + timedelta(days=1),
            ingested_at=self.end + timedelta(days=1),
            available_at=self.end + timedelta(days=1), symbols=("TST",),
        )
        selected, excluded = select_point_in_time([backfill], self.start, self.end)
        self.assertEqual(selected, [])
        self.assertIn("NOT_LOCALLY_AVAILABLE_BY_CUTOFF", excluded[0]["reasons"])

    def test_revision_after_cutoff_cannot_replace_archived_revision(self):
        original = self.article
        revised = NewsArticle(
            provider=original.provider, source_id=original.source_id,
            source_name=original.source_name, source_tier=original.source_tier,
            title="Revised company update", url=original.url,
            published_at=original.published_at,
            updated_at=self.end + timedelta(minutes=1),
            first_seen_at=self.end - timedelta(hours=1),
            ingested_at=self.end - timedelta(hours=1),
            available_at=self.end - timedelta(hours=1), symbols=("TST",),
        )
        selected, excluded = select_point_in_time([original, revised], self.start, self.end)
        self.assertEqual([row.title for row in selected], [original.title])
        self.assertIn("REVISION_AFTER_CUTOFF", excluded[0]["reasons"])

    def test_packet_is_hashed_and_research_can_only_cite_packet_evidence(self):
        packet = evidence_packet("TST", self.start, self.end, [self.article], [
            {"provider": "fixture", "ok": True, "returned": 1, "accepted": 1,
             "gate_eligible": True, "broad_news_coverage": True},
        ])
        client = FundamentalResearchClient.__new__(FundamentalResearchClient)
        client.model = "deepseek-flash"
        client.base_url = "https://api.deepseek.com"
        client.client = SimpleNamespace(responses=_Responses(
            _research_result(self.article.url, self.article.source_id)))
        with patch.dict(os.environ, {"LONGVOL_ALLOW_NATIVE_WEB_SEARCH": "false"}, clear=False):
            result = client.review("TST", json.dumps(packet), True, "2026-09-09")
        self.assertEqual(result["thesis_status"], "intact")
        self.assertEqual(result["retrieval_mode"], "POINT_IN_TIME_PACKET")
        self.assertEqual(result["news_packet_sha256"], packet["packet_sha256"])
        snapshot = FundamentalSnapshot(
            thesis_status="intact", confidence=.8,
            event_class="LIQUIDITY_OR_TECHNICAL_DISLOCATION",
            evidence=(f"{self.article.source_id}: dated evidence",),
            sources=(self.article.url,), source_ids=(self.article.source_id,),
            news_packet_sha256=packet["packet_sha256"],
            point_in_time_collection_ok=True,
        )
        verified, reasons = verify_snapshot_packet(
            snapshot, json.dumps(packet), "TST", "2026-09-09",
        )
        self.assertTrue(verified)
        self.assertEqual(reasons, ())

        tampered_snapshot = FundamentalSnapshot(
            **{**snapshot.__dict__, "source_ids": ("invented",)},
        )
        verified, reasons = verify_snapshot_packet(
            tampered_snapshot, json.dumps(packet), "TST", "2026-09-09",
        )
        self.assertFalse(verified)
        self.assertIn("NEWS_PACKET_SOURCE_ID_MISMATCH", reasons)

    def test_invented_source_fails_closed(self):
        packet = evidence_packet("TST", self.start, self.end, [self.article], [
            {"provider": "fixture", "ok": True, "returned": 1, "accepted": 1,
             "gate_eligible": True, "broad_news_coverage": True},
        ])
        client = FundamentalResearchClient.__new__(FundamentalResearchClient)
        client.model = "deepseek-flash"
        client.base_url = "https://api.deepseek.com"
        client.client = SimpleNamespace(responses=_Responses(
            _research_result("https://invented.example/news", "invented-id")))
        result = client.review("TST", json.dumps(packet), False, "2026-09-09")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["source_ids"], [])
        self.assertEqual(result["thesis_status"], "uncertain")
        self.assertTrue(result["needs_review"])

    def test_tampered_packet_hash_fails_closed(self):
        packet = evidence_packet("TST", self.start, self.end, [self.article], [
            {"provider": "fixture", "ok": True, "returned": 1, "accepted": 1,
             "gate_eligible": True, "broad_news_coverage": True},
        ])
        packet["articles"][0]["title"] = "Tampered after packet generation"
        client = FundamentalResearchClient.__new__(FundamentalResearchClient)
        client.model = "deepseek-flash"
        client.base_url = "https://api.deepseek.com"
        client.client = SimpleNamespace(responses=_Responses(
            _research_result(self.article.url, self.article.source_id)))
        result = client.review("TST", json.dumps(packet), False, "2026-09-09")
        self.assertEqual(result["news_packet_sha256"], "")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["thesis_status"], "uncertain")
        self.assertFalse(result["point_in_time_collection_ok"])

    def test_requested_native_search_without_observed_tool_call_fails_closed(self):
        client = FundamentalResearchClient.__new__(FundamentalResearchClient)
        client.model = "deepseek-flash"
        client.base_url = "https://api.deepseek.com"
        client.client = SimpleNamespace(responses=_Responses(
            _research_result("https://example.com/news", "web-1"), output=[]))
        with patch.dict(os.environ, {"LONGVOL_ALLOW_NATIVE_WEB_SEARCH": "true"}, clear=False):
            result = client.review("TST", "no packet", True, "2026-09-09")
        self.assertTrue(result["native_web_search_requested"])
        self.assertFalse(result["native_web_search_observed"])
        self.assertEqual(result["thesis_status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
