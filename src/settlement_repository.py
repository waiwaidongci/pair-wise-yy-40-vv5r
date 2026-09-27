from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

from .audit import utc_now
from .domain import ConflictError, NotFoundError
from .repository import Repository
from .settlement_rules import (EPSILON, PLAN_PAID, PLAN_UNPAID, REPORT_APPROVED,
                               REPORT_PENDING, REPORT_STATUSES)

# 在事务内完成结算判定，返回(状态, 原因列表, 计划金额)
DecideFn = Callable[[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]],
                    Tuple[str, List[str], Optional[float]]]
# 在事务内按新值重算单条未付计划
RecalcFn = Callable[[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]], float]


class SettlementRepository(Repository):
    """施工结算的进度存档：合同、签证、报量、付款计划与已付记录。"""

    def _create_schema(self) -> None:
        super()._create_schema()
        report_statuses = ",".join("'" + s + "'" for s in REPORT_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_no TEXT NOT NULL UNIQUE,
                    contractor TEXT NOT NULL,
                    total_quantity REAL NOT NULL,
                    unit_price REAL NOT NULL,
                    remark TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','closed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS visas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    visa_no TEXT NOT NULL,
                    quantity_delta REAL NOT NULL DEFAULT 0,
                    amount_delta REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(contract_id, visa_no)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    period TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    visa_no TEXT,
                    remark TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ({report_statuses})),
                    hold_reasons TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_reports_contract ON reports(contract_id);
                CREATE TABLE IF NOT EXISTS payment_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
                    report_id INTEGER NOT NULL UNIQUE REFERENCES reports(id) ON DELETE CASCADE,
                    period TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT '{PLAN_UNPAID}'
                        CHECK(status IN ('{PLAN_UNPAID}','{PLAN_PAID}')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_plans_contract ON payment_plans(contract_id);
                CREATE TABLE IF NOT EXISTS payment_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES payment_plans(id) ON DELETE CASCADE,
                    contract_id INTEGER NOT NULL,
                    period TEXT NOT NULL,
                    amount REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    paid_by TEXT NOT NULL,
                    paid_at TEXT NOT NULL
                );
            """)

    # ---- 合同 ----
    def create_contract(self, contract_no: str, contractor: str, total_quantity: float,
                        unit_price: float, remark: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO contracts(contract_no, contractor, total_quantity, unit_price,
                       remark, status, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,'active',1,?,?,?)""",
                    (contract_no, contractor, total_quantity, unit_price, remark,
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

    def get_contract_by_no(self, contract_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM contracts WHERE contract_no=?", (contract_no,)).fetchone()
        if row is None:
            raise NotFoundError("合同不存在")
        return dict(row)

    def list_contracts(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM contracts ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def correct_contract(self, contract_id: int, total_quantity: float, unit_price: float,
                         expected_version: int, actor: str,
                         recalc_fn: RecalcFn) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """更正合同量/单价，同事务内按新值重算未付计划，已付记录不动。"""
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE contracts SET total_quantity=?, unit_price=?, version=version+1,
                   updated_at=? WHERE id=? AND version=?""",
                (total_quantity, unit_price, now, contract_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM contracts WHERE id=?", (contract_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("合同不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            recalculated = self._recalc_unpaid_locked(contract_id, recalc_fn)
        return self.get_contract(contract_id), recalculated

    def _recalc_unpaid_locked(self, contract_id: int,
                              recalc_fn: RecalcFn) -> List[Dict[str, Any]]:
        """调用方须持有锁并处于事务中；只重算未付计划，已付计划与记录留档。"""
        contract = dict(self.conn.execute(
            "SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone())
        visas = [dict(r) for r in self.conn.execute(
            "SELECT * FROM visas WHERE contract_id=?", (contract_id,)).fetchall()]
        reports = {r["id"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM reports WHERE contract_id=?", (contract_id,)).fetchall()}
        plans = [dict(r) for r in self.conn.execute(
            "SELECT * FROM payment_plans WHERE contract_id=? AND status=?",
            (contract_id, PLAN_UNPAID)).fetchall()]
        now = utc_now()
        changed = []
        for plan in plans:
            report = reports.get(plan["report_id"])
            if report is None:
                continue
            amount = recalc_fn(contract, visas, report)
            if abs(amount - plan["amount"]) > EPSILON:
                self.conn.execute(
                    """UPDATE payment_plans SET amount=?, version=version+1, updated_at=?
                       WHERE id=? AND status=?""",
                    (amount, now, plan["id"], PLAN_UNPAID))
                changed.append({"plan_id": plan["id"], "period": plan["period"],
                                "old_amount": plan["amount"], "new_amount": amount})
        return changed

    # ---- 签证 ----
    def create_visa(self, contract_id: int, visa_no: str, quantity_delta: float,
                    amount_delta: float, reason: str, actor: str) -> Dict[str, Any]:
        self.get_contract(contract_id)
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO visas(contract_id, visa_no, quantity_delta, amount_delta,
                       reason, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,1,?,?,?)""",
                    (contract_id, visa_no, quantity_delta, amount_delta, reason,
                     actor, now, now),
                )
                visa_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("签证编号已存在") from exc
        return self.get_visa(visa_id)

    def get_visa(self, visa_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM visas WHERE id=?", (visa_id,)).fetchone()
        if row is None:
            raise NotFoundError("签证不存在")
        return dict(row)

    def list_visas(self, contract_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM visas WHERE contract_id=? ORDER BY id",
                (contract_id,)).fetchall()
        return [dict(row) for row in rows]

    def correct_visa(self, visa_id: int, quantity_delta: float, amount_delta: float,
                     expected_version: int, actor: str,
                     recalc_fn: RecalcFn) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """更正签证量/签证额，同事务内按新值重算该合同未付计划。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT contract_id FROM visas WHERE id=?", (visa_id,)).fetchone()
            if row is None:
                raise NotFoundError("签证不存在")
            contract_id = int(row["contract_id"])
            cur = self.conn.execute(
                """UPDATE visas SET quantity_delta=?, amount_delta=?, version=version+1,
                   updated_at=? WHERE id=? AND version=?""",
                (quantity_delta, amount_delta, now, visa_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
            recalculated = self._recalc_unpaid_locked(contract_id, recalc_fn)
        return self.get_visa(visa_id), recalculated

    # ---- 报量 ----
    @staticmethod
    def _report(row: sqlite3.Row) -> Dict[str, Any]:
        report = dict(row)
        report["hold_reasons"] = json.loads(report["hold_reasons"])
        return report

    def _contract_visas_reports_locked(self, contract_id: int) -> Tuple[
            Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """调用方须持有锁；返回合同、签证与报量快照供事务内判定。"""
        row = self.conn.execute(
            "SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
        if row is None:
            raise NotFoundError("合同不存在")
        visas = [dict(r) for r in self.conn.execute(
            "SELECT * FROM visas WHERE contract_id=?", (contract_id,)).fetchall()]
        reports = [self._report(r) for r in self.conn.execute(
            "SELECT * FROM reports WHERE contract_id=?", (contract_id,)).fetchall()]
        return dict(row), visas, reports

    def _insert_plan_locked(self, contract_id: int, report_id: int, period: str,
                            amount: float) -> None:
        now = utc_now()
        self.conn.execute(
            """INSERT INTO payment_plans(contract_id, report_id, period, amount, status,
               version, created_at, updated_at) VALUES(?,?,?,?,?,1,?,?)""",
            (contract_id, report_id, period, amount, PLAN_UNPAID, now, now))

    def submit_report(self, contract_id: int, period: str, quantity: float,
                      visa_no: Optional[str], remark: str, actor: str,
                      decide_fn: DecideFn) -> Dict[str, Any]:
        """提交本期报量：事务内完成结算判定，通过则同事务写入付款计划。"""
        now = utc_now()
        with self._lock, self.conn:
            contract, visas, reports = self._contract_visas_reports_locked(contract_id)
            status, reasons, amount = decide_fn(contract, visas, reports)
            cur = self.conn.execute(
                """INSERT INTO reports(contract_id, period, quantity, visa_no, remark, status,
                   hold_reasons, version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
                (contract_id, period, quantity, visa_no, remark, status,
                 json.dumps(reasons, ensure_ascii=False), actor, now, now),
            )
            report_id = int(cur.lastrowid)
            if status == REPORT_APPROVED and amount is not None:
                self._insert_plan_locked(contract_id, report_id, period, amount)
        return self.get_report(report_id)

    def get_report(self, report_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise NotFoundError("报量单不存在")
        return self._report(row)

    def list_reports(self, contract_id: Optional[int] = None,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM reports"
        clauses = []
        params: List[Any] = []
        if contract_id is not None:
            clauses.append("contract_id=?")
            params.append(contract_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._report(row) for row in rows]

    def recheck_report(self, report_id: int, expected_version: int, actor: str,
                       decide_fn: DecideFn) -> Dict[str, Any]:
        """更正后重新核对：仍不通过则停留在待确认并更新原因，通过则写入付款计划。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if row is None:
                raise NotFoundError("报量单不存在")
            current = self._report(row)
            if current["status"] != REPORT_PENDING:
                raise ConflictError("仅待确认报量可重新核对")
            if current["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            contract, visas, reports = self._contract_visas_reports_locked(
                current["contract_id"])
            # 重复判定只看待确认/已核准中先于本单提交的报量，避免与后到的重复单互相锁死
            others = [r for r in reports if r["id"] < report_id]
            status, reasons, amount = decide_fn(contract, visas, others)
            self.conn.execute(
                """UPDATE reports SET status=?, hold_reasons=?, version=version+1,
                   updated_at=? WHERE id=?""",
                (status, json.dumps(reasons, ensure_ascii=False), now, report_id),
            )
            if status == REPORT_APPROVED and amount is not None:
                self._insert_plan_locked(current["contract_id"], report_id,
                                         current["period"], amount)
        return self.get_report(report_id)

    # ---- 付款计划与已付记录 ----
    def get_plan(self, plan_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM payment_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("付款计划不存在")
        return dict(row)

    def list_plans(self, contract_id: Optional[int] = None,
                   status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM payment_plans"
        clauses = []
        params: List[Any] = []
        if contract_id is not None:
            clauses.append("contract_id=?")
            params.append(contract_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def pay_plan(self, plan_id: int, expected_version: int, note: str,
                 actor: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """支付计划：同事务写入已付记录留档，留档金额此后不再重算。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM payment_plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise NotFoundError("付款计划不存在")
            plan = dict(row)
            if plan["version"] != expected_version:
                raise ConflictError("版本冲突，请刷新后重试")
            if plan["status"] != PLAN_UNPAID:
                raise ConflictError("该计划已支付")
            self.conn.execute(
                """UPDATE payment_plans SET status=?, version=version+1, updated_at=?
                   WHERE id=?""",
                (PLAN_PAID, now, plan_id),
            )
            cur = self.conn.execute(
                """INSERT INTO payment_records(plan_id, contract_id, period, amount, note,
                   paid_by, paid_at) VALUES(?,?,?,?,?,?,?)""",
                (plan_id, plan["contract_id"], plan["period"], plan["amount"], note,
                 actor, now),
            )
            record_id = int(cur.lastrowid)
        return self.get_plan(plan_id), self.get_payment_record(record_id)

    def get_payment_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM payment_records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("已付记录不存在")
        return dict(row)

    def list_payment_records(self, contract_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM payment_records"
        params: tuple = ()
        if contract_id is not None:
            sql += " WHERE contract_id=?"
            params = (contract_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
