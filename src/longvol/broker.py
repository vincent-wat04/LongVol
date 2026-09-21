from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo


LIVE_CONFIRMATION = "I_UNDERSTAND_LIVE_ORDERS"


def _number(value, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _records(value) -> list[dict]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        return list(value.to_dict("records"))
    if isinstance(value, dict):
        return [value]
    return [dict(item) for item in value]


@dataclass(frozen=True)
class BrokerConfig:
    environment: Literal["SIMULATE", "REAL"] = "SIMULATE"
    market: Literal["US"] = "US"
    account_id: int | None = None
    security_firm: str = "NONE"
    base_currency: Literal["USD"] = "USD"
    equity_field: str = "usd_net_cash_power"
    strategy_equity_fraction: float = 1.0
    strategy_equity_cap_usd: float | None = 2500.0
    allow_order_submission: bool = True
    allow_live_orders: bool = False
    require_limit_orders: bool = True
    order_type: str = "NORMAL"
    time_in_force: str = "DAY"
    max_entry_risk_fraction: float = 0.03
    max_entry_premium_fraction: float = 0.08
    cold_start_max_entry_risk_fraction: float = 0.025
    cold_start_max_entry_premium_fraction: float = 0.05
    cold_start_max_contracts: int = 1
    estimated_fees_per_contract: float = 2.0
    max_entry_spread_pct: float = 0.12
    max_exit_spread_pct: float = 0.30
    max_entry_price_drift_pct: float = 0.10
    max_entry_chase_atr: float = 0.50
    max_quote_age_seconds: int = 20
    entry_start_et: str = "10:00"
    entry_end_et: str = "15:30"
    max_signal_age_calendar_days: int = 4
    forced_expiry_exit_dte: int = 5
    monitor_poll_seconds: int = 60


@dataclass(frozen=True)
class AccountSnapshot:
    observed_at: str
    environment: str
    account_id: int
    base_currency: str
    equity_field: str
    raw_equity: float
    strategy_equity: float
    usd_cash: float | None = None
    usd_buying_power: float | None = None
    usd_assets: float | None = None
    total_assets_usd: float | None = None
    risk_status: str = ""
    is_pdt: bool | None = None
    day_trades_left: str = ""
    requested_equity_field: str = ""
    equity_fallback_reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LiveQuote:
    code: str
    observed_at: datetime
    quote_time: datetime | None
    last: float
    bid: float
    ask: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    iv: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask >= self.bid else self.last

    @property
    def spread_pct(self) -> float:
        return ((self.ask - self.bid) / self.mid
                if self.mid > 0 and self.bid > 0 and self.ask >= self.bid else float("inf"))


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    code: str
    side: Literal["BUY", "SELL"]
    quantity: int
    limit_price: float
    purpose: Literal["ENTRY", "RISK_EXIT", "TARGET_EXIT"]
    multiplier: int = 100
    underlying_code: str = ""
    reference_spot: float | None = None
    reference_option_ask: float | None = None
    entry_atr: float | None = None
    hard_stop_spot: float | None = None
    option_stop_price: float | None = None
    signal_date: str = ""
    metadata: dict | None = None
    # The operational premium stop is stored in ``option_stop_price``.  The
    # modeled hard-stop exit bid remains the planned-loss/R denominator.
    planned_option_exit_bid: float | None = None


def load_broker_config(path: str | Path) -> BrokerConfig:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("trading config must be a JSON object")
    allowed = {item.name for item in fields(BrokerConfig)}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown trading config keys: {', '.join(unknown)}")
    config = BrokerConfig(**value)
    if config.environment not in {"SIMULATE", "REAL"} or config.market != "US":
        raise ValueError("only US SIMULATE or REAL trading is supported")
    if config.base_currency != "USD":
        raise ValueError("the US-option strategy base currency must be USD")
    for name in ("strategy_equity_fraction", "max_entry_risk_fraction",
                 "max_entry_premium_fraction", "cold_start_max_entry_risk_fraction",
                 "cold_start_max_entry_premium_fraction",
                 "max_entry_spread_pct", "max_exit_spread_pct",
                 "max_entry_price_drift_pct"):
        if not 0 < float(getattr(config, name)) <= 1:
            raise ValueError(f"{name} must be in (0, 1]")
    if config.strategy_equity_cap_usd is not None and config.strategy_equity_cap_usd <= 0:
        raise ValueError("strategy_equity_cap_usd must be positive or null")
    if config.require_limit_orders and config.order_type != "NORMAL":
        raise ValueError("require_limit_orders permits only NORMAL limit orders")
    if config.environment == "SIMULATE" and config.time_in_force != "DAY":
        raise ValueError("Moomoo paper trading supports DAY orders only")
    if config.max_quote_age_seconds <= 0 or config.monitor_poll_seconds < 15:
        raise ValueError("quote and polling intervals are invalid")
    if config.estimated_fees_per_contract < 0:
        raise ValueError("estimated_fees_per_contract cannot be negative")
    if config.cold_start_max_contracts <= 0:
        raise ValueError("cold_start_max_contracts must be positive")
    if config.forced_expiry_exit_dte < 0:
        raise ValueError("forced_expiry_exit_dte cannot be negative")
    for value in (config.entry_start_et, config.entry_end_et):
        datetime.strptime(value, "%H:%M")
    return config


def load_order_intent(path: str | Path) -> OrderIntent:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("order intent must be a JSON object")
    allowed = {item.name for item in fields(OrderIntent)}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown order intent keys: {', '.join(unknown)}")
    for key in ("quantity", "multiplier"):
        if key in value:
            numeric = float(value[key])
            if not math.isfinite(numeric) or not numeric.is_integer():
                raise ValueError(f"{key} must be a whole number")
            value[key] = int(numeric)
    for key in ("limit_price", "reference_spot", "reference_option_ask",
                "entry_atr", "hard_stop_spot", "option_stop_price",
                "planned_option_exit_bid"):
        if value.get(key) is not None:
            value[key] = float(value[key])
    intent = OrderIntent(**value)
    validate_order_intent(intent)
    return intent


def validate_order_intent(intent: OrderIntent) -> None:
    if not intent.client_order_id or len(intent.client_order_id) > 64:
        raise ValueError("client_order_id is required and must be at most 64 characters")
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:" for ch in intent.client_order_id):
        raise ValueError("client_order_id contains unsupported characters")
    if not intent.code.startswith("US."):
        raise ValueError("only US.* order codes are supported")
    quantity = _number(intent.quantity)
    multiplier = _number(intent.multiplier)
    limit_price = _number(intent.limit_price)
    if (intent.side not in {"BUY", "SELL"} or quantity is None or
            quantity <= 0 or not quantity.is_integer()):
        raise ValueError("order side or quantity is invalid")
    if intent.purpose not in {"ENTRY", "RISK_EXIT", "TARGET_EXIT"}:
        raise ValueError("unknown order purpose")
    if (limit_price is None or limit_price <= 0 or multiplier is None or
            multiplier <= 0 or not multiplier.is_integer()):
        raise ValueError("limit price and multiplier must be positive")
    if intent.purpose == "ENTRY" and intent.side != "BUY":
        raise ValueError("ENTRY must be a BUY")
    if intent.purpose == "ENTRY" and (intent.option_stop_price is None or
                                       not 0 <= intent.option_stop_price < intent.limit_price):
        raise ValueError("ENTRY requires an option_stop_price below the limit")
    if (intent.purpose == "ENTRY" and intent.planned_option_exit_bid is not None and
            not 0 <= intent.planned_option_exit_bid < intent.limit_price):
        raise ValueError("ENTRY planned_option_exit_bid must be below the limit")
    if intent.purpose == "ENTRY":
        if (not intent.underlying_code.startswith("US.") or
                intent.reference_spot is None or intent.reference_spot <= 0 or
                intent.reference_option_ask is None or intent.reference_option_ask <= 0 or
                intent.entry_atr is None or intent.entry_atr <= 0 or
                intent.hard_stop_spot is None or
                not 0 < intent.hard_stop_spot < intent.reference_spot):
            raise ValueError("ENTRY requires valid frozen underlying, quote, ATR, and stop fields")
        try:
            datetime.fromisoformat(intent.signal_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("ENTRY requires an ISO signal_date") from exc
        signal = (intent.metadata or {}).get("signal")
        required = {"symbol", "selected_option_expiry", "scenario_target_option_price",
                    "active_iv_percentile", "iv_gate_source", "sizing_mode",
                    "target_spot", "target_source",
                    "selected_option_iv", "max_holding_days", "atr"}
        if not isinstance(signal, dict) or any(signal.get(key) in (None, "") for key in required):
            raise ValueError("ENTRY metadata.signal is incomplete for fill materialization")
        mode = signal["sizing_mode"]
        expected_status = ("PILOT_CANDIDATE" if mode == "COLD_START_FIXED_RISK"
                           else "BUY_CANDIDATE" if mode == "VALIDATED_KELLY" else "")
        if not expected_status or signal.get("status") != expected_status:
            raise ValueError("ENTRY signal status does not match its sizing mode")
    if intent.purpose.endswith("EXIT") and intent.side != "SELL":
        raise ValueError("an exit must be a SELL")
    if intent.purpose.endswith("EXIT"):
        position = (intent.metadata or {}).get("position")
        if not intent.underlying_code.startswith("US.") or not isinstance(position, dict):
            raise ValueError("an exit requires underlying_code and metadata.position")


def validate_live_order(intent: OrderIntent, account: AccountSnapshot,
                        option_quote: LiveQuote, now: datetime,
                        config: BrokerConfig, underlying_quote: LiveQuote | None = None) -> None:
    """Apply last-moment execution and capital checks before submission."""
    validate_order_intent(intent)
    now = now.astimezone(ZoneInfo("America/New_York"))
    timestamp = option_quote.quote_time
    if timestamp is None:
        raise RuntimeError("option quote has no source timestamp")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("America/New_York"))
    age = (now - timestamp.astimezone(now.tzinfo)).total_seconds()
    if age < 0 or age > config.max_quote_age_seconds:
        raise RuntimeError("option quote is stale")
    if option_quote.bid <= 0 or option_quote.ask < option_quote.bid:
        raise RuntimeError("option quote has no executable two-sided market")
    if intent.purpose == "ENTRY":
        if underlying_quote is None or underlying_quote.last <= 0:
            raise RuntimeError("entry requires a positive live underlying quote")
        underlying_time = underlying_quote.quote_time
        if underlying_time is None:
            raise RuntimeError("underlying quote has no source timestamp")
        if underlying_time.tzinfo is None:
            underlying_time = underlying_time.replace(tzinfo=ZoneInfo("America/New_York"))
        underlying_age = (now - underlying_time.astimezone(now.tzinfo)).total_seconds()
        if underlying_age < 0 or underlying_age > config.max_quote_age_seconds:
            raise RuntimeError("underlying quote is stale")
        signal = ((intent.metadata or {}).get("signal") or {})
        sizing_mode = str(signal.get("sizing_mode") or "")
        if sizing_mode not in {"COLD_START_FIXED_RISK", "VALIDATED_KELLY"}:
            raise RuntimeError("entry has no recognized sizing mode")
        premium_cap = config.max_entry_premium_fraction
        risk_cap = config.max_entry_risk_fraction
        if sizing_mode == "COLD_START_FIXED_RISK":
            premium_cap = min(premium_cap, config.cold_start_max_entry_premium_fraction)
            risk_cap = min(risk_cap, config.cold_start_max_entry_risk_fraction)
            if intent.quantity > config.cold_start_max_contracts:
                raise RuntimeError("cold-start entry exceeds the contract cap")
        notional = intent.limit_price * intent.multiplier * intent.quantity
        entry_fees = config.estimated_fees_per_contract * intent.quantity
        capital_at_risk = notional + entry_fees
        if capital_at_risk > account.strategy_equity * premium_cap + 1e-9:
            raise RuntimeError("entry premium exceeds the account-synchronized order cap")
        if (account.usd_buying_power is not None and
                capital_at_risk > account.usd_buying_power):
            raise RuntimeError("entry premium exceeds USD cash buying power")
        planned_exit_bid = (
            float(intent.planned_option_exit_bid)
            if intent.planned_option_exit_bid is not None else
            float(intent.option_stop_price)
        )
        planned_risk = ((intent.limit_price - planned_exit_bid) *
                        intent.multiplier * intent.quantity +
                        2 * config.estimated_fees_per_contract * intent.quantity)
        if planned_risk > account.strategy_equity * risk_cap + 1e-9:
            raise RuntimeError("entry planned loss to the frozen option stop exceeds the account risk cap")
        if option_quote.spread_pct > config.max_entry_spread_pct:
            raise RuntimeError("entry spread is too wide")
        if intent.limit_price > option_quote.ask * (1 + config.max_entry_price_drift_pct):
            raise RuntimeError("entry limit exceeds the live-ask drift allowance")
        if intent.reference_option_ask and option_quote.ask > intent.reference_option_ask * (1 + config.max_entry_price_drift_pct):
            raise RuntimeError("live ask drifted too far from the frozen signal")
        if underlying_quote is not None:
            if intent.hard_stop_spot and underlying_quote.last <= intent.hard_stop_spot:
                raise RuntimeError("underlying has broken the frozen entry stop")
            if (intent.reference_spot and intent.entry_atr and
                    underlying_quote.last > intent.reference_spot + config.max_entry_chase_atr * intent.entry_atr):
                raise RuntimeError("underlying exceeds the no-chase limit")
    else:
        if intent.limit_price > option_quote.bid + 1e-9:
            raise RuntimeError("exit limit must be marketable at or below the current bid")
        if intent.purpose == "TARGET_EXIT" and option_quote.spread_pct > config.max_entry_spread_pct:
            raise RuntimeError("target exit waits for a normal spread")
        # A risk exit is never rejected solely because the spread is wide.


def normalize_account_snapshot(account: dict, funds: dict, config: BrokerConfig,
                               observed_at: datetime) -> AccountSnapshot:
    account_id = int(account.get("acc_id") or account.get("account_id") or 0)
    if account_id <= 0:
        raise ValueError("Moomoo account did not return a stable acc_id")
    requested_equity_field = config.equity_field
    resolved_equity_field = requested_equity_field
    equity_fallback_reason = ""
    raw_equity = _number(funds.get(requested_equity_field))
    # Moomoo's US STOCK_AND_OPTION paper account can expose a real USD cash
    # balance while omitting cashInfoList.netCashPower. The Python SDK then
    # represents usd_net_cash_power as "N/A". For paper trading only, use the
    # explicit USD cash value as a conservative, non-margin bankroll source.
    # REAL accounts remain fail-closed when their requested field is absent.
    if (raw_equity is None or raw_equity <= 0) and (
        config.environment == "SIMULATE"
        and requested_equity_field == "usd_net_cash_power"
    ):
        simulated_us_cash = _number(funds.get("us_cash"))
        if simulated_us_cash is not None and simulated_us_cash > 0:
            raw_equity = simulated_us_cash
            resolved_equity_field = "us_cash"
            equity_fallback_reason = "SIMULATE_NET_CASH_POWER_UNAVAILABLE"
    if raw_equity is None or raw_equity <= 0:
        raise ValueError(f"funds response has no positive {requested_equity_field}")
    strategy_equity = raw_equity * config.strategy_equity_fraction
    if config.strategy_equity_cap_usd is not None:
        strategy_equity = min(strategy_equity, config.strategy_equity_cap_usd)
    if strategy_equity <= 0:
        raise ValueError("resolved strategy equity is not positive")
    raw_pdt = funds.get("is_pdt")
    is_pdt = None if raw_pdt in (None, "", "N/A") else str(raw_pdt).lower() in {"true", "1", "yes"}
    return AccountSnapshot(
        observed_at=observed_at.isoformat(), environment=config.environment,
        account_id=account_id, base_currency=config.base_currency,
        equity_field=resolved_equity_field, raw_equity=raw_equity,
        strategy_equity=strategy_equity,
        usd_cash=_number(funds.get("us_cash")),
        usd_buying_power=_number(funds.get("usd_net_cash_power")),
        usd_assets=_number(funds.get("usd_assets")),
        total_assets_usd=_number(funds.get("total_assets")),
        risk_status=str(funds.get("risk_status") or ""),
        is_pdt=is_pdt, day_trades_left=str(funds.get("pdt_seq") or ""),
        requested_equity_field=requested_equity_field,
        equity_fallback_reason=equity_fallback_reason,
    )


class MoomooBroker:
    """Thin, fail-closed adapter around OpenSecTradeContext.

    Query methods may be called repeatedly. Order submission is intentionally
    never retried because a timeout does not prove that Moomoo rejected it.
    """

    def __init__(self, config: BrokerConfig, host: str = "127.0.0.1", port: int = 11111,
                 *, context=None, sdk=None):
        if sdk is None:
            try:
                import moomoo as sdk  # type: ignore
            except ImportError as exc:
                raise RuntimeError("install moomoo-api and start OpenD before broker operations") from exc
        self.config = config
        self.sdk = sdk
        self._ctx = context
        if self._ctx is None:
            security_firm = getattr(sdk.SecurityFirm, config.security_firm, None)
            if security_firm is None:
                raise ValueError(f"unknown Moomoo security_firm: {config.security_firm}")
            self._ctx = sdk.OpenSecTradeContext(
                filter_trdmarket=getattr(sdk.TrdMarket, config.market),
                host=host, port=port, security_firm=security_firm,
            )
        self.account: dict | None = None

    def close(self) -> None:
        self._ctx.close()

    def _query(self, method, **kwargs):
        result = method(**kwargs)
        if not result or result[0] != self.sdk.RET_OK:
            raise RuntimeError(f"Moomoo trade query failed: {result}")
        return result[1]

    @property
    def trd_env(self):
        return getattr(self.sdk.TrdEnv, self.config.environment)

    def select_account(self) -> dict:
        rows = _records(self._query(self._ctx.get_acc_list))
        matches = []
        for row in rows:
            environment = str(row.get("trd_env") or "").upper()
            if environment and self.config.environment not in environment:
                continue
            account_id = int(row.get("acc_id") or row.get("account_id") or 0)
            if self.config.account_id is not None and account_id != self.config.account_id:
                continue
            sim_type = str(row.get("sim_acc_type") or "").upper()
            if (self.config.environment == "SIMULATE" and
                    not sim_type.endswith("STOCK_AND_OPTION")):
                continue
            matches.append(row)
        if not matches:
            raise RuntimeError("no matching Moomoo trading account")
        if len(matches) > 1:
            raise RuntimeError("multiple matching accounts; set account_id explicitly")
        self.account = matches[0]
        return self.account

    def _account_id(self) -> int:
        account = self.account or self.select_account()
        return int(account.get("acc_id") or account.get("account_id"))

    def account_snapshot(self, observed_at: datetime) -> AccountSnapshot:
        account = self.account or self.select_account()
        currency = getattr(self.sdk.Currency, self.config.base_currency)
        data = self._query(
            self._ctx.accinfo_query, trd_env=self.trd_env, acc_id=self._account_id(),
            refresh_cache=True, currency=currency,
        )
        rows = _records(data)
        if len(rows) != 1:
            raise RuntimeError("Moomoo funds query must return exactly one account row")
        return normalize_account_snapshot(account, rows[0], self.config, observed_at)

    def positions(self) -> list[dict]:
        return _records(self._query(
            self._ctx.position_list_query, trd_env=self.trd_env,
            acc_id=self._account_id(), refresh_cache=True,
        ))

    def orders(self) -> list[dict]:
        return _records(self._query(
            self._ctx.order_list_query, trd_env=self.trd_env,
            acc_id=self._account_id(), refresh_cache=True,
        ))

    def history_orders(self, start: date, end: date) -> list[dict]:
        if start > end:
            raise ValueError("historical order start must not exceed end")
        return _records(self._query(
            self._ctx.history_order_list_query, start=start.isoformat(),
            end=end.isoformat(), trd_env=self.trd_env,
            acc_id=self._account_id(),
        ))

    def existing_order(self, client_order_id: str) -> dict | None:
        return next((row for row in self.orders()
                     if str(row.get("remark") or "") == client_order_id), None)

    def unlock_real_trade(self) -> None:
        if self.config.environment != "REAL":
            return
        password_md5 = os.getenv("MOOMOO_TRADE_PASSWORD_MD5", "")
        if not password_md5:
            raise RuntimeError("REAL trading requires MOOMOO_TRADE_PASSWORD_MD5")
        result = self._ctx.unlock_trade(password_md5=password_md5)
        if not result or result[0] != self.sdk.RET_OK:
            raise RuntimeError(f"Moomoo trade unlock failed: {result}")

    def _authorize_mutation(self, live_confirmation: str) -> None:
        if not self.config.allow_order_submission:
            raise RuntimeError("order submission is disabled in trading config")
        if self.config.environment == "REAL":
            if not self.config.allow_live_orders:
                raise RuntimeError("REAL order submission is disabled in trading config")
            if live_confirmation != LIVE_CONFIRMATION:
                raise RuntimeError("REAL order requires the explicit live confirmation phrase")
            self.unlock_real_trade()

    def place_order(self, intent: OrderIntent, *, submit: bool,
                    live_confirmation: str = "") -> dict:
        validate_order_intent(intent)
        duplicate = self.existing_order(intent.client_order_id)
        if duplicate:
            return {"submitted": False, "duplicate": True, "order": duplicate}
        if not submit:
            return {"submitted": False, "duplicate": False, "dry_run": True,
                    "intent": asdict(intent)}
        self._authorize_mutation(live_confirmation)
        side = getattr(self.sdk.TrdSide, intent.side)
        order_type = getattr(self.sdk.OrderType, self.config.order_type)
        time_in_force = getattr(self.sdk.TimeInForce, self.config.time_in_force)
        try:
            result = self._ctx.place_order(
                price=float(intent.limit_price), qty=int(intent.quantity), code=intent.code,
                trd_side=side, order_type=order_type, trd_env=self.trd_env,
                acc_id=self._account_id(), remark=intent.client_order_id,
                time_in_force=time_in_force,
            )
        except Exception as exc:
            raise RuntimeError("Moomoo order submission raised with unknown state") from exc
        if not result or result[0] != self.sdk.RET_OK:
            raise RuntimeError(f"Moomoo order submission failed or has unknown state: {result}")
        rows = _records(result[1])
        if len(rows) != 1:
            raise RuntimeError("Moomoo order response did not contain exactly one order")
        return {"submitted": True, "duplicate": False, "order": rows[0]}

    def cancel_order(self, order_id: str, *, submit: bool,
                     live_confirmation: str = "") -> dict:
        """Cancel a known broker order; like submission, this is never retried."""
        if not order_id:
            raise ValueError("order_id is required for cancellation")
        if not submit:
            return {"cancelled": False, "dry_run": True, "order_id": order_id}
        self._authorize_mutation(live_confirmation)
        operation = getattr(self.sdk.ModifyOrderOp, "CANCEL")
        result = self._ctx.modify_order(
            modify_order_op=operation, order_id=order_id, qty=0, price=0,
            trd_env=self.trd_env, acc_id=self._account_id(),
        )
        if not result or result[0] != self.sdk.RET_OK:
            raise RuntimeError(f"Moomoo order cancellation failed or has unknown state: {result}")
        rows = _records(result[1])
        if len(rows) != 1:
            raise RuntimeError("Moomoo cancel response did not contain exactly one order")
        return {"cancelled": True, "order": rows[0]}

    def modify_order_price(self, order_id: str, quantity: int, price: float, *,
                           submit: bool, live_confirmation: str = "") -> dict:
        """Move one active limit order; the mutation is intentionally never retried."""
        if not order_id or quantity <= 0 or not math.isfinite(price) or price <= 0:
            raise ValueError("valid order_id, quantity, and price are required")
        if not submit:
            return {"modified": False, "dry_run": True, "order_id": order_id,
                    "quantity": quantity, "price": price}
        self._authorize_mutation(live_confirmation)
        operation = getattr(self.sdk.ModifyOrderOp, "NORMAL")
        try:
            result = self._ctx.modify_order(
                modify_order_op=operation, order_id=order_id,
                qty=int(quantity), price=float(price), trd_env=self.trd_env,
                acc_id=self._account_id(),
            )
        except Exception as exc:
            raise RuntimeError("Moomoo order modification raised with unknown state") from exc
        if not result or result[0] != self.sdk.RET_OK:
            raise RuntimeError(f"Moomoo order modification failed or has unknown state: {result}")
        rows = _records(result[1])
        if len(rows) != 1:
            raise RuntimeError("Moomoo modify response did not contain exactly one order")
        return {"modified": True, "order": rows[0]}
