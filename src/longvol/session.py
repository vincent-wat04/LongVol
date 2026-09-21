"""Trading-session guards for the end-of-day workflow."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


DAILY_READY_TIME = time(17, 0)
PREOPEN_CATCHUP_CUTOFF = time(9, 0)
MAX_CATCHUP_CALENDAR_DAYS = 4


def resolve_daily_as_of(
    now: datetime,
    requested: str | date | None,
    *,
    allow_nonstandard_time: bool = False,
) -> date:
    """Resolve and validate an end-of-day session date.

    Same-day runs retain the 17:00 New York safety gate. A prior-session run
    is permitted only before 09:00 New York and only for a recent weekday.
    Downstream OHLCV and option-source timestamps still have to prove that the
    requested date is the latest completed trading session.
    """
    as_of = (date.fromisoformat(requested) if isinstance(requested, str)
             else requested) or now.date()
    if as_of > now.date():
        raise ValueError("as-of date cannot be in the future")

    if as_of == now.date():
        if now.weekday() >= 5 and not allow_nonstandard_time:
            raise ValueError("refusing a same-day run on a weekend")
        if now.time() < DAILY_READY_TIME and not allow_nonstandard_time:
            raise ValueError(
                "refusing a same-day run before 17:00 America/New_York; "
                "use --as-of YYYY-MM-DD before 09:00 for a prior-session catch-up"
            )
        return as_of

    if as_of.weekday() >= 5:
        raise ValueError("historical as-of must be a weekday trading-session candidate")
    if now.time() >= PREOPEN_CATCHUP_CUTOFF:
        raise ValueError(
            "prior-session catch-up must start before 09:00 America/New_York "
            "so current option quotes cannot contaminate the requested session"
        )
    if as_of < now.date() - timedelta(days=MAX_CATCHUP_CALENDAR_DAYS):
        raise ValueError(
            "full option catch-up is limited to the most recent completed session; "
            "older dates support bars-only synchronization"
        )
    return as_of


def account_snapshot_matches_session(observed_at: datetime, as_of: date) -> bool:
    """Accept same-session funds or next-session pre-open funds for sizing.

    A prior-session catch-up represents a signal generated after that close for
    possible execution on the next session. The broker balance therefore must
    be the current pre-open balance, not a fabricated historical balance.
    """
    if observed_at.tzinfo is None:
        observed_et = observed_at.replace(tzinfo=ZoneInfo("America/New_York"))
    else:
        observed_et = observed_at.astimezone(ZoneInfo("America/New_York"))
    if observed_et.date() == as_of:
        return True
    return (
        as_of < observed_et.date()
        and observed_et.time() < PREOPEN_CATCHUP_CUTOFF
        and as_of.weekday() < 5
        and as_of >= observed_et.date() - timedelta(days=MAX_CATCHUP_CALENDAR_DAYS)
    )
