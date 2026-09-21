import tempfile
import unittest
from datetime import date, datetime, timedelta

from longvol.metrics import aligned_option_panic_metrics
from longvol.models import Bar, Candidate, Config, OptionQuote
from longvol.option_snapshots import (archive_option_snapshot,
                                      load_option_snapshot,
                                      option_snapshot_path)
from longvol.strategy import evaluate


def _panic_bars() -> tuple[list[Bar], date, date]:
    start = date(2026, 1, 1)
    values = [
        Bar(start + timedelta(days=i), 100, 101, 99, 100,
            100 + (i % 3) * 10)
        for i in range(42)
    ]
    shock_day = start + timedelta(days=42)
    values.append(Bar(shock_day, 80, 90, 68, 70, 2000))
    values.append(Bar(shock_day + timedelta(days=1), 70, 72, 69, 71, 400))
    entry_day = shock_day + timedelta(days=2)
    values.append(Bar(entry_day, 71, 73, 70, 72, 300))
    return values, shock_day, entry_day


def _surface(day: date, stressed: bool) -> list[OptionQuote]:
    observed = datetime.combine(day, datetime.min.time())
    near = day + timedelta(days=45)
    far = day + timedelta(days=120)
    put_iv = .70 if stressed else .40
    call_iv = .50 if stressed else .42
    near_iv = .65 if stressed else .42
    far_iv = .55 if stressed else .43
    put_volume = 300 if stressed else 10
    call_volume = 20 if stressed else 100
    return [
        OptionQuote("A-P25", near, 65, "P", 1.0, 1.1, 1.05,
                    put_iv, -.25, .02, 1000, put_volume,
                    quote_time=observed),
        OptionQuote("A-C25", near, 79, "C", 1.0, 1.1, 1.05,
                    call_iv, .25, .02, 1000, call_volume,
                    quote_time=observed),
        OptionQuote("A-C50", near, 72, "C", 2.0, 2.1, 2.05,
                    near_iv, .50, .03, 1000, call_volume,
                    quote_time=observed),
        OptionQuote("A-C50-FAR", far, 72, "C", 3.0, 3.1, 3.05,
                    far_iv, .50, .02, 1000, call_volume,
                    quote_time=observed),
    ]


class OptionPanicAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.bars, self.shock_day, self.entry_day = _panic_bars()
        self.config = Config(
            min_history_bars=20, min_option_open_interest=0,
            min_option_volume=0, option_mc_enabled=False,
        )

    def test_historical_shock_never_uses_entry_chain_as_backfill(self):
        evaluation = evaluate(
            Candidate("A", self.entry_day), self.bars,
            _surface(self.entry_day, stressed=True), None, None, self.config,
        )

        self.assertTrue(evaluation.hard_gates["price_flow"])
        self.assertFalse(evaluation.metrics["option_panic_gate"])
        self.assertFalse(evaluation.metrics["option_panic_known"])
        self.assertEqual(evaluation.metrics["option_panic_status"], "UNKNOWN")
        self.assertEqual(
            evaluation.metrics["shock_option_alignment_status"],
            "MISSING_SHOCK_SNAPSHOT",
        )
        self.assertEqual(
            evaluation.metrics["shock_option_panic_source"],
            "MISSING_HISTORICAL_ARCHIVE",
        )
        self.assertFalse(evaluation.hard_gates["panic_confirmed"])
        self.assertEqual(evaluation.metrics["entry_option_snapshot_day"],
                         self.entry_day.isoformat())
        self.assertIsNone(evaluation.metrics["shock_option_snapshot_day"])

    def test_aligned_archive_confirms_shock_but_entry_surface_stays_current(self):
        evaluation = evaluate(
            Candidate("A", self.entry_day), self.bars,
            _surface(self.entry_day, stressed=False), None, None, self.config,
            shock_options=_surface(self.shock_day, stressed=True),
            shock_option_day=self.shock_day,
        )

        self.assertTrue(evaluation.metrics["option_panic_known"])
        self.assertTrue(evaluation.metrics["option_panic_gate"])
        self.assertTrue(evaluation.hard_gates["panic_confirmed"])
        self.assertEqual(evaluation.metrics["shock_option_alignment_status"],
                         "ALIGNED")
        self.assertEqual(evaluation.metrics["shock_option_snapshot_day"],
                         self.shock_day.isoformat())
        self.assertAlmostEqual(evaluation.metrics["put_call_25d_skew"], -.02)
        self.assertAlmostEqual(
            evaluation.metrics["shock_option_put_call_25d_skew"], .20,
        )

    def test_mislabeled_historical_snapshot_fails_closed(self):
        result = aligned_option_panic_metrics(
            _surface(self.entry_day, stressed=True), 70, self.shock_day,
            self.entry_day,
        )
        self.assertFalse(result["option_panic_gate"])
        self.assertFalse(result["option_panic_known"])
        self.assertEqual(result["option_panic_alignment_status"],
                         "SNAPSHOT_DAY_MISMATCH")

    def test_downside_yz_can_be_second_confirmation_when_options_unknown(self):
        start = date(2026, 1, 1)
        prices = [100.0] * 60 + [100 - i * .5 for i in range(1, 20)]
        values = [
            Bar(start + timedelta(days=i), price, price * 1.01,
                price * .99, price, 100 + (i % 3) * 10)
            for i, price in enumerate(prices)
        ]
        shock_day = start + timedelta(days=len(values))
        values.append(Bar(shock_day, 85, 86, 68, 70, 2000))
        values.append(Bar(shock_day + timedelta(days=1), 70, 72, 69, 71, 400))
        entry_day = shock_day + timedelta(days=2)
        values.append(Bar(entry_day, 71, 73, 70, 72, 300))

        evaluation = evaluate(
            Candidate("A", entry_day), values,
            _surface(entry_day, stressed=False), None, None, self.config,
        )
        self.assertEqual(evaluation.metrics["option_panic_status"], "UNKNOWN")
        self.assertTrue(evaluation.metrics["downside_return_share"] >= .60)
        self.assertTrue(evaluation.metrics["yang_zhang_ratio"] >= 1.25)
        self.assertTrue(evaluation.hard_gates["panic_confirmed"])

    def test_same_day_shock_reuses_aligned_entry_chain(self):
        shock_bars = self.bars[:-2]
        evaluation = evaluate(
            Candidate("A", self.shock_day), shock_bars,
            _surface(self.shock_day, stressed=True), None, None, self.config,
        )
        self.assertTrue(evaluation.metrics["option_panic_gate"])
        self.assertEqual(evaluation.metrics["shock_option_panic_source"],
                         "ENTRY_CHAIN_SAME_DAY")
        self.assertEqual(evaluation.metrics["shock_option_snapshot_day"],
                         self.shock_day.isoformat())


class OptionSnapshotArchiveTests(unittest.TestCase):
    def test_archive_round_trip_and_missing_are_distinguishable(self):
        day = date(2026, 9, 9)
        with tempfile.TemporaryDirectory() as directory:
            missing, missing_day = load_option_snapshot(directory, "A", day)
            self.assertIsNone(missing)
            self.assertIsNone(missing_day)

            path = archive_option_snapshot(
                directory, "A", day, _surface(day, stressed=True),
            )
            self.assertEqual(path, option_snapshot_path(directory, "A", day))
            restored, restored_day = load_option_snapshot(directory, "A", day)
            self.assertEqual(restored_day, day)
            self.assertEqual(len(restored or []), 4)
            self.assertEqual(restored[0].quote_time.date(), day)

    def test_archive_is_idempotent_but_cannot_rewrite_history(self):
        day = date(2026, 9, 9)
        with tempfile.TemporaryDirectory() as directory:
            original = _surface(day, stressed=True)
            path = archive_option_snapshot(directory, "A", day, original)
            first_bytes = path.read_bytes()
            archive_option_snapshot(directory, "A", day, list(reversed(original)))
            self.assertEqual(path.read_bytes(), first_bytes)
            with self.assertRaisesRegex(FileExistsError, "immutable option snapshot"):
                archive_option_snapshot(
                    directory, "A", day, _surface(day, stressed=False),
                )

    def test_archive_rejects_unproven_or_mismatched_quote_day(self):
        day = date(2026, 9, 9)
        unproven = OptionQuote(
            "A-C", day + timedelta(days=90), 100, "C", 1, 1.1, 1.05,
            .5, .4, .02, 1000, 100,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "cannot be proven"):
                archive_option_snapshot(directory, "A", day, [unproven])
            with self.assertRaisesRegex(ValueError, "does not match"):
                archive_option_snapshot(
                    directory, "A", day,
                    _surface(day + timedelta(days=1), stressed=True),
                )


if __name__ == "__main__":
    unittest.main()
