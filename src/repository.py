from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    contractor TEXT NOT NULL,
                    total_quantity REAL NOT NULL,
                    unit_price REAL NOT NULL,
                    visa_quantity REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    period TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    remark TEXT NOT NULL DEFAULT '',
                    visa_ref TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending','approved')),
                    reasons TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
                    period TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unpaid'
                        CHECK(status IN ('unpaid','paid')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES payment_plans(id) ON DELETE CASCADE,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    amount REAL NOT NULL,
                    paid_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    # ---- 施工结算 ----
    def create_contract(self, contract_no: str, title: str, contractor: str,
                        total_quantity: float, unit_price: float,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO contracts(contract_no, title, contractor, total_quantity,
                       unit_price, visa_quantity, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,0,1,?,?,?)""",
                    (contract_no, title, contractor, total_quantity, unit_price,
                     actor, now, now),
                )
                contract_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("合同编号已存在") from exc
        return self.get_contract(contract_id)

    def get_contract(self, contract_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if row is None:
            raise NotFoundError("合同不存在")
        return dict(row)

    def list_contracts(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM contracts ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def update_contract(self, contract_id: int, total_quantity: float,
                        visa_quantity: float, unit_price: float,
                        expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE contracts SET total_quantity=?, visa_quantity=?, unit_price=?,
                   version=version+1, updated_at=? WHERE id=? AND version=?""",
                (total_quantity, visa_quantity, unit_price, now,
                 contract_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM contracts WHERE id=?", (contract_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("合同不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_contract(contract_id)

    def create_submission(self, contract_id: int, period: str, quantity: float,
                          remark: str, visa_ref: Optional[str], status: str,
                          reasons: List[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO submissions(contract_id, period, quantity, remark, visa_ref,
                   status, reasons, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (contract_id, period, quantity, remark, visa_ref, status,
                 json.dumps(reasons, ensure_ascii=False), actor, now),
            )
            submission_id = int(cur.lastrowid)
        return self.get_submission(submission_id)

    def get_submission(self, submission_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if row is None:
            raise NotFoundError("报量不存在")
        return dict(row)

    def list_submissions(self, contract_id: int,
                         status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM submissions WHERE contract_id=?"
        params: tuple = (contract_id,)
        if status:
            sql += " AND status=?"
            params = (contract_id, status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def approved_quantity(self, contract_id: int) -> float:
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(quantity),0) AS q FROM submissions
                   WHERE contract_id=? AND status='approved'""",
                (contract_id,),
            ).fetchone()
        return float(row["q"])

    def submission_periods(self, contract_id: int,
                           exclude_id: Optional[int] = None) -> List[str]:
        sql = """SELECT period FROM submissions
                 WHERE contract_id=? AND status IN ('pending','approved')"""
        params: tuple = (contract_id,)
        if exclude_id is not None:
            sql += " AND id<>?"
            params = (contract_id, exclude_id)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [row["period"] for row in rows]

    def update_submission_status(self, submission_id: int, status: str,
                                 reasons: List[str]) -> Dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE submissions SET status=?, reasons=? WHERE id=?",
                (status, json.dumps(reasons, ensure_ascii=False), submission_id),
            )
        return self.get_submission(submission_id)

    def create_payment_plan(self, contract_id: int, submission_id: int, period: str,
                            quantity: float, amount: float) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO payment_plans(contract_id, submission_id, period, quantity,
                   amount, status, version, created_at, updated_at)
                   VALUES(?,?,?,?,?,'unpaid',1,?,?)""",
                (contract_id, submission_id, period, quantity, amount, now, now),
            )
            plan_id = int(cur.lastrowid)
        return self.get_payment_plan(plan_id)

    def get_payment_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM payment_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("付款计划不存在")
        return dict(row)

    def list_payment_plans(self, contract_id: int,
                           status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM payment_plans WHERE contract_id=?"
        params: tuple = (contract_id,)
        if status:
            sql += " AND status=?"
            params = (contract_id, status)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def recalc_unpaid_plans(self, contract_id: int, unit_price: float) -> List[int]:
        now = utc_now()
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT id, quantity FROM payment_plans WHERE contract_id=? AND status='unpaid'",
                (contract_id,),
            ).fetchall()
            ids = []
            for row in rows:
                amount = round(float(row["quantity"]) * float(unit_price), 2)
                self.conn.execute(
                    """UPDATE payment_plans SET amount=?, version=version+1, updated_at=?
                       WHERE id=? AND status='unpaid'""",
                    (amount, now, row["id"]),
                )
                ids.append(int(row["id"]))
        return ids

    def mark_plan_paid(self, plan_id: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE payment_plans SET status='paid', version=version+1, updated_at=?
                   WHERE id=? AND status='unpaid'""",
                (now, plan_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM payment_plans WHERE id=?", (plan_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("付款计划不存在")
                raise ConflictError("该计划已支付")
        return self.get_payment_plan(plan_id)

    def create_payment(self, plan_id: int, contract_id: int, amount: float,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO payments(plan_id, contract_id, amount, paid_by, created_at)
                   VALUES(?,?,?,?,?)""",
                (plan_id, contract_id, amount, actor, now),
            )
            payment_id = int(cur.lastrowid)
            row = self.conn.execute(
                "SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        return dict(row)

    def list_payments(self, contract_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM payments WHERE contract_id=? ORDER BY id",
                (contract_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
