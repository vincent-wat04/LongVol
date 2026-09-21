"""Point-in-time financial-news collection.

The language model is deliberately downstream of this module.  Collectors use
fixed vendor endpoints, normalize timestamps, enforce the requested cutoff and
produce an immutable evidence packet.  This makes "what the model could have
known" auditable and prevents a model-generated URL from becoming evidence.

News API access does not by itself grant the right to send full article text to
a third-party model.  The collectors therefore request/use headlines and
summaries by default; deployments must separately verify their data licence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("news timestamps must include a timezone")
    return value.astimezone(UTC)


def _parse_timestamp(value: object, *, assume_tz=UTC) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value), tz=UTC)
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("missing publication timestamp")
        if raw.isdigit() and len(raw) in {10, 13}:
            stamp = float(raw) / (1000 if len(raw) == 13 else 1)
            return datetime.fromtimestamp(stamp, tz=UTC)
        # SEC acceptanceDateTime commonly uses YYYYMMDDHHMMSS.
        if raw.isdigit() and len(raw) == 14:
            result = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=NY)
        else:
            try:
                result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                result = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    if result.tzinfo is None:
        result = result.replace(tzinfo=assume_tz)
    return result.astimezone(UTC)


def cutoff_end(as_of: str | date | datetime) -> datetime:
    """Return an inclusive UTC point-in-time cutoff.

    A date means the end of the New York calendar day.  Callers that need an
    intraday cutoff must pass a timezone-aware datetime.
    """
    if isinstance(as_of, datetime):
        return _utc(as_of)
    day = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    return datetime.combine(day, time.max, tzinfo=NY).astimezone(UTC)


@dataclass(frozen=True)
class NewsArticle:
    provider: str
    source_id: str
    source_name: str
    source_tier: str
    title: str
    url: str
    published_at: datetime
    updated_at: datetime | None = None
    first_seen_at: datetime | None = None
    ingested_at: datetime | None = None
    available_at: datetime | None = None
    symbols: tuple[str, ...] = ()
    summary: str = ""
    content_hash: str = ""
    event_type: str = "NEWS"
    point_in_time_status: str = "LIVE_CAPTURE"
    tombstone: bool = False

    def __post_init__(self) -> None:
        if not self.provider or not self.source_id or not self.title:
            raise ValueError("news provider, source_id and title are required")
        if not self.url.startswith(("https://", "http://")):
            raise ValueError("news evidence requires a direct HTTP(S) URL")
        object.__setattr__(self, "published_at", _utc(self.published_at))
        for field_name in ("updated_at", "first_seen_at", "ingested_at", "available_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _utc(value))
        object.__setattr__(self, "symbols", tuple(sorted({s.upper() for s in self.symbols if s})))
        if not self.content_hash:
            raw = "\n".join((self.title.strip(), self.summary.strip(), self.url.strip()))
            object.__setattr__(self, "content_hash", hashlib.sha256(raw.encode("utf-8")).hexdigest())
        if self.first_seen_at is None and self.ingested_at is not None:
            object.__setattr__(self, "first_seen_at", self.ingested_at)
        if self.available_at is None:
            object.__setattr__(self, "available_at", self.first_seen_at or self.ingested_at)
        if self.point_in_time_status not in {
            "LIVE_CAPTURE", "PROVIDER_DELIVERY_TIME", "BACKFILL_NON_PIT", "PIT_UNKNOWN",
        }:
            raise ValueError("invalid point_in_time_status")

    def as_dict(self) -> dict:
        row = asdict(self)
        for key in ("published_at", "updated_at", "first_seen_at", "ingested_at", "available_at"):
            value = row[key]
            row[key] = value.isoformat().replace("+00:00", "Z") if value else None
        row["symbols"] = list(self.symbols)
        return row


def article_from_dict(row: dict) -> NewsArticle:
    return NewsArticle(
        provider=str(row["provider"]), source_id=str(row["source_id"]),
        source_name=str(row.get("source_name") or ""),
        source_tier=str(row.get("source_tier") or "UNKNOWN"),
        title=str(row["title"]), url=str(row["url"]),
        published_at=_parse_timestamp(row["published_at"]),
        updated_at=_parse_timestamp(row["updated_at"]) if row.get("updated_at") else None,
        first_seen_at=_parse_timestamp(row["first_seen_at"]) if row.get("first_seen_at") else None,
        ingested_at=_parse_timestamp(row["ingested_at"]) if row.get("ingested_at") else None,
        available_at=_parse_timestamp(row["available_at"]) if row.get("available_at") else None,
        symbols=tuple(row.get("symbols") or ()), summary=str(row.get("summary") or ""),
        content_hash=str(row.get("content_hash") or ""),
        event_type=str(row.get("event_type") or "NEWS"),
        point_in_time_status=str(row.get("point_in_time_status") or "PIT_UNKNOWN"),
        tombstone=bool(row.get("tombstone", False)),
    )


class NewsProvider(Protocol):
    name: str

    def fetch(self, symbol: str, start: datetime, end: datetime) -> list[NewsArticle]: ...


def _get_json(url: str, headers: dict[str, str], timeout: float = 20) -> object:
    request = Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec: fixed provider URLs
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Never include response bodies or authentication material in errors.
        raise RuntimeError(f"news provider returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"news provider request failed: {type(exc).__name__}") from exc


def _get_text(url: str, headers: dict[str, str], timeout: float = 20,
              max_bytes: int = 1_000_000) -> str:
    request = Request(url, headers={"Accept": "text/html,text/plain", **headers})
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec: fixed provider URLs
            return response.read(max_bytes + 1)[:max_bytes].decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"news source returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"news source request failed: {type(exc).__name__}") from exc


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.values.append(data.strip())


def _plain_text(value: str, limit: int = 8_000) -> str:
    parser = _VisibleText()
    try:
        parser.feed(value)
        text = " ".join(parser.values)
    except Exception:
        text = value
    return " ".join(text.split())[:limit]


class AlpacaNewsProvider:
    name = "alpaca_benzinga_news"
    broad_news_coverage = True

    def __init__(self, key_id: str, secret_key: str, *,
                 realtime_entitled: bool = False, timeout: float = 20):
        if not key_id or not secret_key:
            raise ValueError("Alpaca news requires key id and secret key")
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        # Alpaca documents a 15-minute boundary for accounts without real-time
        # access. HTTP success alone therefore cannot prove cutoff coverage.
        self.gate_eligible = bool(realtime_entitled)
        self.timeout = timeout

    def fetch(self, symbol: str, start: datetime, end: datetime) -> list[NewsArticle]:
        base_query = {
            "symbols": symbol.upper(), "start": _utc(start).isoformat(),
            "end": _utc(end).isoformat(), "sort": "asc", "limit": 50,
            "include_content": "false",
        }
        rows = []
        page_token = ""
        for _page in range(20):
            query = urlencode({**base_query, **({"page_token": page_token} if page_token else {})})
            payload = _get_json(
                f"https://data.alpaca.markets/v1beta1/news?{query}",
                self.headers, self.timeout,
            )
            if not isinstance(payload, dict):
                raise RuntimeError("Alpaca news returned an invalid page")
            rows.extend(payload.get("news", []))
            page_token = str(payload.get("next_page_token") or "")
            if not page_token:
                break
        else:
            raise RuntimeError("Alpaca news pagination exceeded 20 pages")
        ingested = datetime.now(UTC)
        result = []
        for row in rows:
            try:
                published = _parse_timestamp(row.get("created_at"))
                result.append(NewsArticle(
                    provider=self.name, source_id=str(row.get("id") or row.get("url") or ""),
                    source_name=str(row.get("source") or "Benzinga via Alpaca"),
                    source_tier="LICENSED_NEWS", title=str(row.get("headline") or "").strip(),
                    url=str(row.get("url") or ""), published_at=published,
                    updated_at=_parse_timestamp(row["updated_at"]) if row.get("updated_at") else None,
                    first_seen_at=ingested, ingested_at=ingested, available_at=ingested,
                    symbols=tuple(row.get("symbols") or ()), summary=str(row.get("summary") or ""),
                ))
            except (TypeError, ValueError):
                continue
        return result


class MassiveNewsProvider:
    """Massive (formerly Polygon.io) reference-news collector."""

    name = "massive_reference_news"
    gate_eligible = False  # The documented base feed is updated hourly.
    broad_news_coverage = True

    def __init__(self, api_key: str, *, timeout: float = 20):
        if not api_key:
            raise ValueError("Massive news requires an API key")
        self.api_key = api_key
        self.timeout = timeout

    def fetch(self, symbol: str, start: datetime, end: datetime) -> list[NewsArticle]:
        query = urlencode({
            "ticker": symbol.upper(), "published_utc.gte": _utc(start).isoformat(),
            "published_utc.lte": _utc(end).isoformat(), "order": "asc",
            "sort": "published_utc", "limit": 1000, "apiKey": self.api_key,
        })
        next_url = f"https://api.massive.com/v2/reference/news?{query}"
        rows = []
        for _page in range(10):
            payload = _get_json(next_url, {}, self.timeout)
            if not isinstance(payload, dict):
                raise RuntimeError("Massive news returned an invalid page")
            rows.extend(payload.get("results", []))
            next_url = str(payload.get("next_url") or "")
            if not next_url:
                break
            if not next_url.startswith("https://api.massive.com/"):
                raise RuntimeError("Massive pagination returned an unexpected host")
            if "apiKey=" not in next_url:
                next_url += ("&" if "?" in next_url else "?") + urlencode({"apiKey": self.api_key})
        else:
            raise RuntimeError("Massive news pagination exceeded 10 pages")
        ingested = datetime.now(UTC)
        result = []
        for row in rows:
            publisher = row.get("publisher") if isinstance(row.get("publisher"), dict) else {}
            try:
                published = _parse_timestamp(row.get("published_utc"))
                result.append(NewsArticle(
                    provider=self.name, source_id=str(row.get("id") or row.get("article_url") or ""),
                    source_name=str(publisher.get("name") or "Massive news"),
                    source_tier="AGGREGATED_NEWS", title=str(row.get("title") or "").strip(),
                    url=str(row.get("article_url") or ""), published_at=published,
                    first_seen_at=ingested, ingested_at=ingested, available_at=ingested,
                    symbols=tuple(row.get("tickers") or ()), summary=str(row.get("description") or ""),
                ))
            except (TypeError, ValueError):
                continue
        return result


class BenzingaNewsProvider:
    """Direct Benzinga Newsfeed collector using abstract-only output."""

    name = "benzinga_newsfeed"
    broad_news_coverage = True

    def __init__(self, api_key: str, *, realtime_entitled: bool = False,
                 timeout: float = 20):
        if not api_key:
            raise ValueError("Benzinga news requires an API key")
        self.api_key = api_key
        # Benzinga products/contracts can differ. Require the operator to
        # attest that this credential has complete real-time news entitlement.
        self.gate_eligible = bool(realtime_entitled)
        self.timeout = timeout

    def fetch(self, symbol: str, start: datetime, end: datetime) -> list[NewsArticle]:
        rows = []
        for page in range(20):
            query = urlencode({
                "tickers": symbol.upper(),
                "dateFrom": _utc(start).date().isoformat(), "dateTo": _utc(end).date().isoformat(),
                "displayOutput": "abstract", "pageSize": 100, "page": page,
                "sort": "created:asc",
            })
            payload = _get_json(
                f"https://api.benzinga.com/api/v2/news?{query}",
                {"Authorization": f"token {self.api_key}"}, self.timeout,
            )
            page_rows = (payload.get("news", payload.get("data", [])) if isinstance(payload, dict)
                         else payload if isinstance(payload, list) else [])
            rows.extend(page_rows)
            if len(page_rows) < 100:
                break
        else:
            raise RuntimeError("Benzinga news pagination exceeded 20 pages")
        ingested = datetime.now(UTC)
        result = []
        for row in rows:
            stocks = row.get("stocks") or row.get("tickers") or ()
            symbols = tuple(str(x.get("name") or x.get("symbol") or "")
                            if isinstance(x, dict) else str(x) for x in stocks)
            try:
                published = _parse_timestamp(row.get("created") or row.get("published"))
                result.append(NewsArticle(
                    provider=self.name, source_id=str(row.get("id") or row.get("url") or ""),
                    source_name="Benzinga", source_tier="LICENSED_NEWS",
                    title=str(row.get("title") or row.get("headline") or "").strip(),
                    url=str(row.get("url") or ""), published_at=published,
                    updated_at=_parse_timestamp(row["updated"]) if row.get("updated") else None,
                    first_seen_at=ingested, ingested_at=ingested, available_at=ingested, symbols=symbols,
                    summary=str(row.get("teaser") or row.get("abstract") or ""),
                ))
            except (TypeError, ValueError):
                continue
        return result


class SecEdgarProvider:
    """Primary-source SEC filing collector (filings, not a general news feed)."""

    name = "sec_edgar_submissions"
    gate_eligible = True
    broad_news_coverage = False
    _material_forms = {
        "8-K", "8-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A", "NT 10-K", "NT 10-Q",
        "6-K", "6-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
        "S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A",
        "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "EFFECT", "RW",
        "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "DEF 14A", "25-NSE",
    }

    def __init__(self, user_agent: str, *, timeout: float = 20):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must identify the application and a contact email")
        self.headers = {"User-Agent": user_agent}
        self.timeout = timeout
        self._ticker_map: dict[str, int] | None = None

    def _cik(self, symbol: str) -> int | None:
        if self._ticker_map is None:
            mapping = _get_json("https://www.sec.gov/files/company_tickers.json", self.headers, self.timeout)
            companies = mapping.values() if isinstance(mapping, dict) else mapping if isinstance(mapping, list) else []
            self._ticker_map = {
                str(row.get("ticker") or "").upper(): int(row["cik_str"])
                for row in companies if row.get("ticker") and row.get("cik_str") is not None
            }
        return self._ticker_map.get(symbol.upper())

    def fetch(self, symbol: str, start: datetime, end: datetime) -> list[NewsArticle]:
        cik = self._cik(symbol)
        if cik is None:
            return []
        payload = _get_json(
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json", self.headers, self.timeout)
        recent = payload.get("filings", {}).get("recent", {}) if isinstance(payload, dict) else {}
        keys = ("accessionNumber", "filingDate", "acceptanceDateTime", "form",
                "primaryDocument", "primaryDocDescription")
        count = max((len(recent.get(key, [])) for key in keys), default=0)
        ingested = datetime.now(UTC)
        result = []
        for index in range(count):
            def value(key: str) -> str:
                rows = recent.get(key, [])
                return str(rows[index]) if index < len(rows) and rows[index] is not None else ""
            form = value("form")
            if form not in self._material_forms:
                continue
            try:
                raw_time = value("acceptanceDateTime") or f"{value('filingDate')}T16:00:00-04:00"
                published = _parse_timestamp(raw_time, assume_tz=NY)
                if not _utc(start) <= published <= _utc(end):
                    continue
                accession = value("accessionNumber")
                document = value("primaryDocument")
                if not accession or not document or "/" in document or "\\" in document:
                    continue
                archive = accession.replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{archive}/{document}"
                try:
                    excerpts = [_plain_text(_get_text(url, self.headers, self.timeout), 6_000)]
                except RuntimeError:
                    # Filing metadata alone is not enough for a fundamental
                    # classification, so omit an unreadable document.
                    continue
                index_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{archive}/index.json"
                try:
                    index = _get_json(index_url, self.headers, self.timeout)
                    items = index.get("directory", {}).get("item", []) if isinstance(index, dict) else []
                    exhibits = [str(item.get("name") or "") for item in items
                                if isinstance(item, dict) and
                                str(item.get("name") or "").lower().startswith(("ex99", "ex-99"))]
                    for exhibit in exhibits[:2]:
                        if exhibit and "/" not in exhibit and "\\" not in exhibit:
                            exhibit_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{archive}/{exhibit}"
                            excerpts.append(_plain_text(
                                _get_text(exhibit_url, self.headers, self.timeout), 4_000))
                except RuntimeError:
                    # The primary filing is still valid evidence when an index
                    # or optional exhibit fetch is unavailable.
                    pass
                summary = " | ".join(value for value in excerpts if value)[:12_000]
                result.append(NewsArticle(
                    provider=self.name, source_id=accession, source_name="U.S. SEC EDGAR",
                    source_tier="PRIMARY_REGULATORY", title=f"SEC {form}: {value('primaryDocDescription') or document}",
                    url=url, published_at=published, first_seen_at=ingested,
                    ingested_at=ingested, available_at=ingested,
                    symbols=(symbol.upper(),), summary=(summary or
                    f"Accepted SEC filing {form} for {symbol.upper()}."),
                    event_type="SEC_FILING",
                ))
            except (TypeError, ValueError):
                continue
        return result


def configured_providers(selection: str | None = None) -> list[NewsProvider]:
    """Resolve providers without silently downgrading a named provider."""
    requested = (selection or os.getenv("LONGVOL_NEWS_PROVIDERS", "auto")).lower()
    if requested in {"", "none", "off"}:
        return []
    if requested == "auto":
        names = []
        if os.getenv("BENZINGA_API_KEY"):
            names.append("benzinga")
        elif os.getenv("APCA_API_KEY_ID") and os.getenv("APCA_API_SECRET_KEY"):
            names.append("alpaca")
        elif os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_API_SECRET"):
            names.append("alpaca")
        elif os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY"):
            names.append("massive")
        if os.getenv("SEC_USER_AGENT"):
            names.append("sec")
    else:
        names = [name.strip() for name in requested.split(",") if name.strip()]
    providers: list[NewsProvider] = []
    for name in names:
        if name == "benzinga":
            providers.append(BenzingaNewsProvider(
                os.getenv("BENZINGA_API_KEY", ""),
                realtime_entitled=_env_true("BENZINGA_NEWS_REALTIME_ENTITLED"),
            ))
        elif name == "alpaca":
            providers.append(AlpacaNewsProvider(
                os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY", ""),
                os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET", ""),
                realtime_entitled=_env_true("ALPACA_NEWS_REALTIME_ENTITLED"),
            ))
        elif name in {"massive", "polygon"}:
            providers.append(MassiveNewsProvider(
                os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY", "")))
        elif name == "sec":
            providers.append(SecEdgarProvider(os.getenv("SEC_USER_AGENT", "")))
        else:
            raise ValueError(f"unknown news provider: {name}")
    return providers


def provider_configuration(selection: str | None = None) -> dict:
    """Describe news credentials without making a network request.

    This is deliberately separate from a capability probe: health checks may
    expose configuration mistakes, but must never incur vendor or model usage.
    Secrets are represented only as booleans.
    """
    requested = (selection or os.getenv("LONGVOL_NEWS_PROVIDERS", "auto")).lower()
    if requested in {"", "none", "off"}:
        names: list[str] = []
    elif requested == "auto":
        names = []
        if os.getenv("BENZINGA_API_KEY"):
            names.append("benzinga")
        elif ((os.getenv("APCA_API_KEY_ID") and os.getenv("APCA_API_SECRET_KEY")) or
              (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_API_SECRET"))):
            names.append("alpaca")
        elif os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY"):
            names.append("massive")
        if os.getenv("SEC_USER_AGENT"):
            names.append("sec")
    else:
        names = [name.strip() for name in requested.split(",") if name.strip()]

    rows = []
    for name in names:
        normalized = "massive" if name == "polygon" else name
        if normalized == "benzinga":
            configured = bool(os.getenv("BENZINGA_API_KEY"))
            broad = True
            entitlement_confirmed = _env_true("BENZINGA_NEWS_REALTIME_ENTITLED")
            gate_eligible = entitlement_confirmed
        elif normalized == "alpaca":
            configured = bool(
                (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY")) and
                (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET"))
            )
            broad = True
            entitlement_confirmed = _env_true("ALPACA_NEWS_REALTIME_ENTITLED")
            gate_eligible = entitlement_confirmed
        elif normalized == "massive":
            configured = bool(os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY"))
            broad, gate_eligible = True, False
            entitlement_confirmed = False
        elif normalized == "sec":
            configured = bool(os.getenv("SEC_USER_AGENT"))
            broad, gate_eligible = False, True
            entitlement_confirmed = True
        else:
            configured = broad = gate_eligible = False
            entitlement_confirmed = False
        rows.append({
            "provider": normalized,
            "configured": configured,
            "broad_news_coverage": broad,
            "gate_eligible": gate_eligible,
            "realtime_entitlement_confirmed": entitlement_confirmed,
        })
    return {
        "selection": requested,
        "providers": rows,
        "all_requested_configured": all(row["configured"] for row in rows),
        "broad_gate_provider_configured": any(
            row["configured"] and row["broad_news_coverage"] and row["gate_eligible"]
            for row in rows
        ),
        "network_probe_run": False,
    }


def read_news_archive(path: str | Path) -> list[NewsArticle]:
    """Read an append-only JSONL archive, failing closed on corrupt history."""
    source = Path(path)
    if not source.exists():
        return []
    rows: list[NewsArticle] = []
    with source.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("archive row is not an object")
                rows.append(article_from_dict(value))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid news archive {source} at line {line_number}: {exc}"
                ) from exc
    return rows


def append_news_archive(path: str | Path, articles: Iterable[NewsArticle]) -> int:
    """Append unseen article revisions and return the number written.

    A provider/source id may legitimately acquire a revised body.  Revisions
    are therefore keyed by content hash and retained, while byte-identical
    observations are not duplicated.
    """
    target = Path(path)
    existing = read_news_archive(target)
    seen = {(row.provider, row.source_id, row.content_hash) for row in existing}
    pending = []
    for article in articles:
        key = (article.provider, article.source_id, article.content_hash)
        if key not in seen:
            pending.append(article)
            seen.add(key)
    if not pending:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for article in pending:
            handle.write(json.dumps(
                article.as_dict(), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(pending)


def collect_news(symbol: str, start: datetime, end: datetime,
                 providers: Iterable[NewsProvider],
                 existing: Iterable[NewsArticle] = ()) -> tuple[list[NewsArticle], list[dict]]:
    """Collect article revisions without pretending a REST backfill was seen live.

    The returned rows are append-only observations, not yet point-in-time
    evidence.  ``select_point_in_time`` performs the cutoff selection.  Existing
    identical revisions retain their original first-seen/available timestamps.
    """
    start, end = _utc(start), _utc(end)
    if start > end:
        raise ValueError("news start must be on or before end")
    articles: list[NewsArticle] = []
    diagnostics: list[dict] = []
    for provider in providers:
        try:
            rows = provider.fetch(symbol.upper(), start, end)
            valid = [row for row in rows if start <= row.published_at <= end and
                     (not row.symbols or symbol.upper() in row.symbols)]
            articles.extend(valid)
            diagnostics.append({
                "provider": provider.name, "ok": True,
                "returned": len(rows), "accepted": len(valid),
                "gate_eligible": bool(getattr(provider, "gate_eligible", False)),
                "broad_news_coverage": bool(getattr(provider, "broad_news_coverage", False)),
            })
        except Exception as exc:
            diagnostics.append({
                "provider": provider.name, "ok": False,
                "gate_eligible": bool(getattr(provider, "gate_eligible", False)),
                "broad_news_coverage": bool(getattr(provider, "broad_news_coverage", False)),
                "error": f"{type(exc).__name__}: {exc}",
            })
    by_revision: dict[tuple[str, str, str], NewsArticle] = {
        (article.provider, article.source_id, article.content_hash): article
        for article in existing
    }
    for article in sorted(articles, key=lambda item: (item.published_at, item.provider, item.source_id)):
        key = (article.provider, article.source_id, article.content_hash)
        old = by_revision.get(key)
        if old is not None:
            article = replace(
                article,
                first_seen_at=old.first_seen_at,
                available_at=old.available_at,
            )
        by_revision[key] = article
    return sorted(by_revision.values(), key=lambda item: (
        item.published_at, item.provider, item.source_id, item.available_at or datetime.max.replace(tzinfo=UTC)
    )), diagnostics


def select_point_in_time(articles: Iterable[NewsArticle], start: datetime,
                         cutoff: datetime) -> tuple[list[NewsArticle], list[dict]]:
    """Select the last locally available revision at or before ``cutoff``.

    Rows without a defensible availability time and revisions first ingested or
    updated after the cutoff are excluded rather than silently backfilled.
    """
    start, cutoff = _utc(start), _utc(cutoff)
    eligible: list[NewsArticle] = []
    excluded: list[dict] = []
    for article in articles:
        reasons = []
        if not start <= article.published_at <= cutoff:
            reasons.append("PUBLICATION_OUTSIDE_WINDOW")
        if article.available_at is None or article.available_at > cutoff:
            reasons.append("NOT_LOCALLY_AVAILABLE_BY_CUTOFF")
        if article.updated_at is not None and article.updated_at > cutoff:
            reasons.append("REVISION_AFTER_CUTOFF")
        if article.point_in_time_status in {"BACKFILL_NON_PIT", "PIT_UNKNOWN"}:
            reasons.append("NON_POINT_IN_TIME_SOURCE")
        if article.tombstone:
            reasons.append("TOMBSTONE")
        if reasons:
            excluded.append({"provider": article.provider, "source_id": article.source_id,
                             "content_hash": article.content_hash, "reasons": reasons})
        else:
            eligible.append(article)
    latest: dict[tuple[str, str], NewsArticle] = {}
    for article in eligible:
        key = (article.provider, article.source_id)
        old = latest.get(key)
        if old is None or (article.available_at, article.updated_at or article.published_at) > (
                old.available_at, old.updated_at or old.published_at):
            latest[key] = article
    return sorted(latest.values(), key=lambda item: item.published_at), excluded


def evidence_packet(symbol: str, start: datetime, end: datetime,
                    articles: Iterable[NewsArticle], diagnostics: list[dict],
                    local_context: str = "", excluded_revisions: list[dict] | None = None) -> dict:
    selected, automatically_excluded = select_point_in_time(articles, start, end)
    rows = [article.as_dict() for article in selected]
    coverage = sorted(({
        "provider": str(row.get("provider") or ""),
        "ok": row.get("ok") is True,
        "gate_eligible": row.get("gate_eligible") is True,
        "broad_news_coverage": row.get("broad_news_coverage") is True,
    } for row in diagnostics if isinstance(row, dict)), key=lambda row: row["provider"])
    signed_payload = {
        "articles": rows,
        "local_context": local_context[:20_000],
        "provider_coverage": coverage,
    }
    canonical = json.dumps(signed_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "schema_version": "1.1",
        "symbol": symbol.upper(),
        "start": _utc(start).isoformat().replace("+00:00", "Z"),
        "cutoff": _utc(end).isoformat().replace("+00:00", "Z"),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "article_count": len(rows),
        "packet_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "provider_diagnostics": diagnostics,
        "provider_coverage": coverage,
        "excluded_revisions": [*(excluded_revisions or []), *automatically_excluded],
        "local_context": local_context[:20_000],
        "articles": rows,
    }
