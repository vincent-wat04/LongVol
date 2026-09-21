from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Candidate, Evaluation, Position


class TradingLog:
    """Append-only research journal backed by SQLite."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit keeps every observation durable even if a later symbol
        # fails; a run status still records whether the batch completed.
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, run_type TEXT NOT NULL,
          data_source TEXT, model TEXT, metadata_json TEXT,
          created_at TEXT NOT NULL, config_sha256 TEXT, config_json TEXT,
          app_version TEXT, status TEXT DEFAULT 'STARTED', error_text TEXT
        );
        CREATE TABLE IF NOT EXISTS candidate_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          symbol TEXT NOT NULL, as_of TEXT NOT NULL, reason TEXT,
          screener_values_json TEXT,
          FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS feature_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          symbol TEXT NOT NULL, as_of TEXT NOT NULL, status TEXT NOT NULL,
          passed_gates INTEGER NOT NULL, gates_json TEXT NOT NULL,
          metrics_json TEXT NOT NULL, selected_option_json TEXT,
          reasons_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS trades (
          trade_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, action TEXT NOT NULL,
          symbol TEXT NOT NULL, option_symbol TEXT, quantity INTEGER,
          spot_price REAL, option_price REAL, fees REAL, thesis TEXT,
          run_id TEXT, features_json TEXT, notes TEXT, broker_order_id TEXT,
          currency TEXT DEFAULT 'USD', multiplier INTEGER DEFAULT 100,
          position_json TEXT,
          FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS exit_decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, as_of TEXT NOT NULL,
          symbol TEXT NOT NULL, option_symbol TEXT NOT NULL, action TEXT NOT NULL,
          reason TEXT NOT NULL, decision_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, level TEXT NOT NULL,
          event_type TEXT NOT NULL, message TEXT NOT NULL, details_json TEXT,
          created_at TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS account_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
          observed_at TEXT NOT NULL, environment TEXT NOT NULL,
          account_id TEXT NOT NULL, base_currency TEXT NOT NULL,
          strategy_equity REAL NOT NULL, snapshot_json TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS broker_orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
          client_order_id TEXT NOT NULL, broker_order_id TEXT,
          environment TEXT NOT NULL, account_id TEXT NOT NULL,
          code TEXT NOT NULL, side TEXT NOT NULL, purpose TEXT NOT NULL,
          quantity INTEGER NOT NULL, limit_price REAL NOT NULL,
          status TEXT NOT NULL, dealt_qty REAL DEFAULT 0,
          dealt_avg_price REAL DEFAULT 0, recorded_qty REAL DEFAULT 0,
          materialized_trade_id TEXT, intent_json TEXT NOT NULL,
          order_json TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runs(run_id),
          UNIQUE(environment, account_id, client_order_id)
        );
        CREATE TABLE IF NOT EXISTS sizing_mode_decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          symbol TEXT NOT NULL, as_of TEXT NOT NULL, mode TEXT NOT NULL,
          validated_ready INTEGER NOT NULL, blockers_json TEXT NOT NULL,
          evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_features_symbol_date ON feature_snapshots(symbol, as_of);
        CREATE INDEX IF NOT EXISTS idx_trades_symbol_date ON trades(symbol, trade_date);
        CREATE INDEX IF NOT EXISTS idx_exits_symbol_date ON exit_decisions(symbol, as_of);
        CREATE INDEX IF NOT EXISTS idx_account_snapshots_time ON account_snapshots(observed_at);
        CREATE INDEX IF NOT EXISTS idx_broker_orders_broker_id ON broker_orders(broker_order_id);
        CREATE INDEX IF NOT EXISTS idx_sizing_mode_symbol_date
          ON sizing_mode_decisions(symbol, as_of);
        """)
        # Add current columns when opening a database created by an older release.
        existing = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        for name, ddl in {
            "run_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "data_source": "TEXT",
            "metadata_json": "TEXT",
            "config_sha256": "TEXT", "config_json": "TEXT", "app_version": "TEXT",
            "status": "TEXT DEFAULT 'STARTED'", "error_text": "TEXT",
        }.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")
        trade_columns = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
        for name, ddl in {
            "broker_order_id": "TEXT", "currency": "TEXT DEFAULT 'USD'",
            "multiplier": "INTEGER DEFAULT 100", "position_json": "TEXT",
        }.items():
            if name not in trade_columns:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_broker_order
                         ON trades(broker_order_id)
                         WHERE broker_order_id IS NOT NULL AND broker_order_id <> ''""")
        broker_order_columns = {r[1] for r in self.db.execute("PRAGMA table_info(broker_orders)")}
        for name, ddl in {
            "recorded_qty": "REAL DEFAULT 0",
            "materialized_trade_id": "TEXT",
        }.items():
            if name not in broker_order_columns:
                self.db.execute(f"ALTER TABLE broker_orders ADD COLUMN {name} {ddl}")
        self.db.commit()

    def new_run(self, as_of: str, run_type: str = "scan", data_source: str = "",
                model: str = "", metadata: dict | None = None,
                config_sha256: str = "", config: dict | None = None,
                app_version: str = "") -> str:
        run_id = str(uuid.uuid4())
        self.db.execute("""INSERT INTO runs
            (run_id,as_of,run_type,data_source,model,metadata_json,created_at,
             config_sha256,config_json,app_version,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, as_of, run_type, data_source, model,
                         json.dumps(metadata or {}, default=str), datetime.now(timezone.utc).isoformat(),
                         config_sha256, json.dumps(config or {}, default=str, sort_keys=True), app_version, "STARTED"))
        self.db.commit()
        return run_id

    def record_candidate(self, run_id: str, candidate: Candidate) -> None:
        self.db.execute("INSERT INTO candidate_snapshots(run_id,symbol,as_of,reason,screener_values_json) VALUES (?,?,?,?,?)",
                        (run_id, candidate.symbol, candidate.as_of.isoformat(), candidate.screener_reason,
                         json.dumps(candidate.screener_values, default=str)))

    def record_evaluation(self, run_id: str, evaluation: Evaluation) -> None:
        selected = evaluation.selected_option
        selected_json = json.dumps(selected.__dict__, default=str) if selected else None
        self.db.execute("INSERT INTO feature_snapshots(run_id,symbol,as_of,status,passed_gates,gates_json,metrics_json,selected_option_json,reasons_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (run_id, evaluation.symbol, evaluation.as_of.isoformat(), evaluation.status, evaluation.passed_gates,
                         json.dumps(evaluation.hard_gates, default=str), json.dumps(evaluation.metrics, default=str), selected_json,
                         json.dumps(evaluation.reasons, default=str), datetime.now(timezone.utc).isoformat()))

    def record_sizing_mode(self, run_id: str, symbol: str, as_of: str,
                           mode: str, validated_ready: bool,
                           blockers: list[str], evidence: dict) -> None:
        self.db.execute("""INSERT INTO sizing_mode_decisions
            (run_id,symbol,as_of,mode,validated_ready,blockers_json,
             evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, symbol, as_of, mode, int(validated_ready),
             json.dumps(blockers, sort_keys=True),
             json.dumps(evidence, default=str, sort_keys=True),
             datetime.now(timezone.utc).isoformat()))

    def latest_sizing_mode(self, symbol: str) -> str | None:
        row = self.db.execute(
            """SELECT mode FROM sizing_mode_decisions WHERE symbol=?
               ORDER BY id DESC LIMIT 1""", (symbol,)).fetchone()
        return str(row[0]) if row else None

    def record_trade(self, action: str, position: Position, trade_date: str, quantity: int,
                     spot_price: float, option_price: float, fees: float = 0.0,
                     run_id: str | None = None, features: dict | None = None, notes: str = "",
                     broker_order_id: str = "", currency: str = "USD") -> str:
        trade_id = str(uuid.uuid4())
        self.db.execute("""INSERT INTO trades
            (trade_id,trade_date,action,symbol,option_symbol,quantity,spot_price,
             option_price,fees,thesis,run_id,features_json,notes,broker_order_id,
             currency,multiplier,position_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, trade_date, action, position.symbol, position.option_symbol, quantity,
             spot_price, option_price, fees, position.thesis, run_id,
             json.dumps(features or {}, default=str), notes, broker_order_id,
             currency, position.multiplier, json.dumps(position.__dict__, default=str, sort_keys=True)))
        self.db.commit()
        return trade_id

    def record_exit_decision(self, run_id: str | None, as_of: str, decision: dict) -> None:
        self.db.execute("""INSERT INTO exit_decisions
            (run_id,as_of,symbol,option_symbol,action,reason,decision_json,created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, as_of, decision.get("symbol", ""), decision.get("option_symbol", ""),
             decision.get("action", "DATA_NEEDED"), decision.get("reason", ""),
             json.dumps(decision, default=str, sort_keys=True), datetime.now(timezone.utc).isoformat()))

    def record_account_snapshot(self, snapshot: dict, run_id: str | None = None) -> None:
        self.db.execute("""INSERT INTO account_snapshots
            (run_id,observed_at,environment,account_id,base_currency,
             strategy_equity,snapshot_json) VALUES (?,?,?,?,?,?,?)""",
            (run_id, snapshot["observed_at"], snapshot["environment"],
             str(snapshot["account_id"]), snapshot["base_currency"],
             float(snapshot["strategy_equity"]),
             json.dumps(snapshot, default=str, sort_keys=True)))

    def record_broker_order(self, intent: dict, result: dict, environment: str,
                            account_id: int, run_id: str | None = None) -> None:
        order = result.get("order") or {}
        now = datetime.now(timezone.utc).isoformat()
        broker_id = str(order.get("order_id") or order.get("orderID") or "")
        status = str(order.get("order_status") or order.get("orderStatus") or
                     ("DRY_RUN" if result.get("dry_run") else "UNKNOWN"))
        dealt_qty = float(order.get("dealt_qty") or order.get("fillQty") or 0)
        dealt_avg_price = float(order.get("dealt_avg_price") or order.get("fillAvgPrice") or 0)
        self.db.execute("""INSERT INTO broker_orders
            (run_id,client_order_id,broker_order_id,environment,account_id,
             code,side,purpose,quantity,limit_price,status,dealt_qty,
             dealt_avg_price,intent_json,order_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(environment,account_id,client_order_id) DO UPDATE SET
              broker_order_id=excluded.broker_order_id,
              status=excluded.status,dealt_qty=excluded.dealt_qty,
              dealt_avg_price=excluded.dealt_avg_price,
              order_json=excluded.order_json,updated_at=excluded.updated_at""",
            (run_id, intent["client_order_id"], broker_id, environment,
             str(account_id), intent["code"], intent["side"], intent["purpose"],
             int(intent["quantity"]), float(intent["limit_price"]), status,
             dealt_qty, dealt_avg_price, json.dumps(intent, default=str, sort_keys=True),
             json.dumps(order, default=str, sort_keys=True), now, now))

    def reconcile_broker_order(self, environment: str, account_id: int,
                               order: dict) -> bool:
        client_order_id = str(order.get("remark") or "")
        if not client_order_id:
            return False
        status = str(order.get("order_status") or order.get("orderStatus") or "UNKNOWN")
        dealt_qty = float(order.get("dealt_qty") or order.get("fillQty") or 0)
        dealt_avg_price = float(order.get("dealt_avg_price") or order.get("fillAvgPrice") or 0)
        broker_id = str(order.get("order_id") or order.get("orderID") or "")
        cursor = self.db.execute("""UPDATE broker_orders SET broker_order_id=?,status=?,
            dealt_qty=?,dealt_avg_price=?,order_json=?,updated_at=?
            WHERE environment=? AND account_id=? AND client_order_id=?""",
            (broker_id, status, dealt_qty, dealt_avg_price,
             json.dumps(order, default=str, sort_keys=True),
             datetime.now(timezone.utc).isoformat(), environment,
             str(account_id), client_order_id))
        return cursor.rowcount > 0

    def pending_fill_rows(self, environment: str, account_id: int) -> list[dict]:
        cursor = self.db.execute("""SELECT * FROM broker_orders
            WHERE environment=? AND account_id=? AND dealt_qty > recorded_qty
            ORDER BY created_at""", (environment, str(account_id)))
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def broker_order(self, environment: str, account_id: int,
                     client_order_id: str) -> dict | None:
        cursor = self.db.execute("""SELECT * FROM broker_orders
            WHERE environment=? AND account_id=? AND client_order_id=?""",
            (environment, str(account_id), client_order_id))
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        return dict(zip(columns, row))

    def trade_for_broker_order(self, broker_order_id: str) -> str | None:
        if not broker_order_id:
            return None
        row = self.db.execute("SELECT trade_id FROM trades WHERE broker_order_id=?",
                              (broker_order_id,)).fetchone()
        return str(row[0]) if row else None

    def trade_record(self, trade_id: str) -> dict | None:
        """Return one durable trade journal row for view reconstruction."""
        cursor = self.db.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in cursor.description]
        return dict(zip(columns, row))

    def mark_fill_materialized(self, row_id: int, quantity: float,
                               trade_id: str) -> None:
        self.db.execute("""UPDATE broker_orders SET recorded_qty=?,
            materialized_trade_id=?,updated_at=? WHERE id=?""",
            (quantity, trade_id, datetime.now(timezone.utc).isoformat(), row_id))
        self.db.commit()

    def event(self, level: str, event_type: str, message: str,
              run_id: str | None = None, details: dict | None = None) -> None:
        self.db.execute("INSERT INTO events(run_id,level,event_type,message,details_json,created_at) VALUES (?,?,?,?,?,?)",
                        (run_id, level, event_type, message, json.dumps(details or {}, default=str),
                         datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def finish_run(self, run_id: str, status: str = "COMPLETED", error_text: str = "") -> None:
        self.db.execute("UPDATE runs SET status=?, error_text=? WHERE run_id=?", (status, error_text, run_id))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
