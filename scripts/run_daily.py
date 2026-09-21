"""Safe scheduler entry point for the after-close workflow."""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from longvol.session import resolve_daily_as_of


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--lookback-days", type=int, default=400)
    parser.add_argument("--config", default="config/strategy.json")
    parser.add_argument("--sizing", default="config/sizing.json")
    parser.add_argument("--trading-config", default="config/trading.json")
    parser.add_argument("--research-model")
    parser.add_argument("--news-providers")
    parser.add_argument("--news-start")
    parser.add_argument("--news-lookback-days", type=int, default=14)
    parser.add_argument("--allow-native-web-search", action="store_true")
    parser.add_argument(
        "--as-of",
        help=("explicit New York trading date (YYYY-MM-DD); before 09:00 ET this "
              "enables a timestamp-validated prior-session catch-up"),
    )
    parser.add_argument("--allow-nonstandard-time", action="store_true")
    args = parser.parse_args()

    now = datetime.now(ZoneInfo("America/New_York"))
    try:
        as_of = resolve_daily_as_of(
            now, args.as_of, allow_nonstandard_time=args.allow_nonstandard_time)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    root = Path(args.root).resolve()
    start = as_of - timedelta(days=args.lookback_days)
    config = Path(args.config)
    if not config.is_absolute():
        config = root / config
    sizing = Path(args.sizing)
    if not sizing.is_absolute():
        sizing = root / sizing
    trading_config = Path(args.trading_config)
    if not trading_config.is_absolute():
        trading_config = root / trading_config
    common = ["--host", args.host, "--port", str(args.port)]
    subprocess.run([
        "longvol", "healthcheck", "--data-dir", str(root / "data"),
        "--state-dir", str(root / "state"), "--opend", "--broker", "--research",
        "--config", str(config), "--sizing", str(sizing),
        "--trading-config", str(trading_config), *common,
        *(["--news-providers", args.news_providers] if args.news_providers else []),
    ], check=True)
    subprocess.run([
        "longvol", "daily", "--as-of", as_of.isoformat(),
        "--start", start.isoformat(), "--root", str(root),
        "--config", str(config), "--sizing", str(sizing),
        "--trading-config", str(trading_config),
        *(["--research-model", args.research_model] if args.research_model else []),
        *(["--news-providers", args.news_providers] if args.news_providers else []),
        *(["--news-start", args.news_start] if args.news_start else []),
        "--news-lookback-days", str(args.news_lookback_days),
        *(["--allow-native-web-search"] if args.allow_native_web_search else []),
        *common,
    ], check=True)


if __name__ == "__main__":
    main()
