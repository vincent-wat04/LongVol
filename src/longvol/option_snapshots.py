from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable

from .io import read_options, write_rows
from .models import OptionQuote


OPTION_SNAPSHOT_COLUMNS = [
    "symbol", "expiry", "strike", "right", "bid", "ask", "last",
    "iv", "delta", "gamma", "open_interest", "volume",
    "iv_percentile", "quote_time", "multiplier", "vega", "theta",
]


def option_snapshot_path(data_dir: str | Path, symbol: str,
                         snapshot_day: date) -> Path:
    """Return the immutable-date location for one underlying's option chain."""
    normalized = symbol.strip().upper()
    if (not normalized or normalized in {".", ".."} or
            "/" in normalized or "\\" in normalized):
        raise ValueError("symbol is not safe for an option snapshot path")
    return (Path(data_dir) / "options_history" /
            snapshot_day.isoformat() / f"{normalized}.csv")


def _validate_snapshot_day(options: Iterable[OptionQuote],
                           snapshot_day: date) -> list[OptionQuote]:
    items = list(options)
    for option in items:
        if option.quote_time is None:
            raise ValueError(
                f"{option.symbol} has no quote_time; snapshot day cannot be proven"
            )
        if option.quote_time.date() != snapshot_day:
            raise ValueError(
                f"{option.symbol} quote day {option.quote_time.date()} does not "
                f"match snapshot day {snapshot_day}"
            )
    return items


def archive_option_snapshot(data_dir: str | Path, symbol: str,
                            snapshot_day: date,
                            options: Iterable[OptionQuote]) -> Path:
    """Atomically archive a timestamp-proven chain for later shock alignment.

    A dated snapshot is immutable. Repeating an identical capture is
    idempotent; a different chain for an already archived day is rejected so a
    rerun cannot rewrite what the strategy claims was observed at that time.
    """
    items = _validate_snapshot_day(options, snapshot_day)
    target = option_snapshot_path(data_dir, symbol, snapshot_day)
    if target.exists():
        existing = _validate_snapshot_day(read_options(target), snapshot_day)
        order = lambda item: (item.symbol, item.expiry, item.strike, item.right)
        if sorted(existing, key=order) != sorted(items, key=order):
            raise FileExistsError(
                f"immutable option snapshot already exists with different content: {target}"
            )
        return target
    rows = [{
        **option.__dict__,
        "expiry": option.expiry.isoformat(),
        "quote_time": option.quote_time.isoformat(sep=" "),
    } for option in items]
    write_rows(target, rows, fieldnames=OPTION_SNAPSHOT_COLUMNS)
    return target


def load_option_snapshot(
    data_dir: str | Path, symbol: str, snapshot_day: date,
) -> tuple[list[OptionQuote] | None, date | None]:
    """Load a dated chain in the shape accepted by ``strategy.evaluate``.

    ``(None, None)`` means no archive exists.  An existing header-only file
    returns ``([], snapshot_day)`` so callers can distinguish a known empty
    observation from missing history.
    """
    target = option_snapshot_path(data_dir, symbol, snapshot_day)
    if not target.exists():
        return None, None
    items = _validate_snapshot_day(read_options(target), snapshot_day)
    return items, snapshot_day
