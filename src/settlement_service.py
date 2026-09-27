from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import settlement_rules as rules
from .domain import ValidationError, ensure_role, require_number, require_text
from .settlement_repository import SettlementRepository


def _expected_version(payload: Dict[str, Any]) -> int:
    expected = payload.get("expected_version")
    if not isinstance(expected, int) or expected < 1:
        raise ValueError("expected_version必须是正整数")
    return expected


def _optional_text(payload: Dict[str, Any], field: str, max_length: int = 2000) -> Optional[str]:
    value = payload.get(field)
    if value is None:
        return None
    return require_text(value, field, max_length)


class SettlementService:
    """施工结算用例编排：提交报量、核对、更正重算、支付留档。"""

    def __init__(self, repository: SettlementRepository):
        self.repository = repository

    # ---- 合同 ----
    def create_contract(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, rules.CONTRACT_WRITE_ROLES)
        actor = require_text(actor, "actor", 100)
        contract_no = require_text(payload.get("contract_no"), "contract_no", 100)
        contractor = require_text(payload.get("contractor"), "contractor", 100)
        total_quantity = require_number(payload.get("total_quantity"), "total_quantity", 0.000001)
        unit_price = require_number(payload.get("unit_price"), "unit_price", 0.000001)
        remark = _optional_text(payload, "remark") or ""
        contract = self.repository.create_contract(
            contract_no, contractor, total_quantity, unit_price, remark, actor)
        self.repository.append_audit("contract_create", rules.ENTITY_CONTRACT,
                                     contract["id"], actor, {
                                         "contract_no": contract_no,
                                         "contractor": contractor,
                                         "total_quantity": total_quantity,
                                         "unit_price": unit_price,
                                     })
        return contract

    def list_contracts(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, rules.SETTLEMENT_VIEW_ROLES)
        return self.repository.list_contracts()

    def correct_contract(self, contract_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, rules.CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        expected = _expected_version(payload)
        current = self.repository.get_contract(contract_id)
        total_quantity = current["total_quantity"]
        unit_price = current["unit_price"]
        changed = False
        if payload.get("total_quantity") is not None:
            total_quantity = require_number(
                payload.get("total_quantity"), "total_quantity", 0.000001)
            changed = True
        if payload.get("unit_price") is not None:
            unit_price = require_number(payload.get("unit_price"), "unit_price", 0.000001)
            changed = True
        if not changed:
            raise ValidationError("至少更正合同量或单价之一")
        contract, recalculated = self.repository.correct_contract(
            contract_id, total_quantity, unit_price, expected, actor,
            rules.recalc_plan_amount)
        self.repository.append_audit("contract_correct", rules.ENTITY_CONTRACT,
                                     contract_id, actor, {
                                         "total_quantity": total_quantity,
                                         "unit_price": unit_price,
                                         "recalculated_plans": recalculated,
                                     })
        return {"contract": contract, "recalculated_plans": recalculated}

    # ---- 签证 ----
    def add_visa(self, contract_id: int, payload: Dict[str, Any], actor: str,
                 role: str) -> Dict[str, Any]:
        ensure_role(role, rules.VISA_WRITE_ROLES)
        actor = require_text(actor, "actor", 100)
        visa_no = require_text(payload.get("visa_no"), "visa_no", 100)
        quantity_delta = require_number(payload.get("quantity_delta", 0), "quantity_delta", -1e15)
        amount_delta = require_number(payload.get("amount_delta", 0), "amount_delta", -1e15)
        reason = require_text(payload.get("reason"), "reason")
        visa = self.repository.create_visa(
            contract_id, visa_no, quantity_delta, amount_delta, reason, actor)
        self.repository.append_audit("visa_create", rules.ENTITY_VISA, visa["id"], actor, {
            "contract_id": contract_id, "visa_no": visa_no,
            "quantity_delta": quantity_delta, "amount_delta": amount_delta,
        })
        return visa

    def correct_visa(self, visa_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, rules.CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        expected = _expected_version(payload)
        current = self.repository.get_visa(visa_id)
        quantity_delta = current["quantity_delta"]
        amount_delta = current["amount_delta"]
        changed = False
        if payload.get("quantity_delta") is not None:
            quantity_delta = require_number(
                payload.get("quantity_delta"), "quantity_delta", -1e15)
            changed = True
        if payload.get("amount_delta") is not None:
            amount_delta = require_number(payload.get("amount_delta"), "amount_delta", -1e15)
            changed = True
        if not changed:
            raise ValidationError("至少更正签证量或签证额之一")
        visa, recalculated = self.repository.correct_visa(
            visa_id, quantity_delta, amount_delta, expected, actor,
            rules.recalc_plan_amount)
        self.repository.append_audit("visa_correct", rules.ENTITY_VISA, visa_id, actor, {
            "contract_id": visa["contract_id"], "quantity_delta": quantity_delta,
            "amount_delta": amount_delta, "recalculated_plans": recalculated,
        })
        return {"visa": visa, "recalculated_plans": recalculated}

    # ---- 报量 ----
    def submit_report(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, rules.REPORT_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        contract_no = require_text(payload.get("contract_no"), "contract_no", 100)
        period = require_text(payload.get("period"), "period", 50)
        quantity = require_number(payload.get("quantity"), "quantity", 0.000001)
        visa_no = _optional_text(payload, "visa_no", 100)
        remark = _optional_text(payload, "remark") or ""
        contract = self.repository.get_contract_by_no(contract_no)
        report = self.repository.submit_report(
            contract["id"], period, quantity, visa_no, remark, actor,
            lambda c, visas, reports: rules.decide_report(
                c, visas, reports, period, quantity, visa_no))
        self.repository.append_audit("report_submit", rules.ENTITY_REPORT,
                                     report["id"], actor, {
                                         "contract_no": contract_no, "period": period,
                                         "quantity": quantity, "status": report["status"],
                                         "hold_reasons": report["hold_reasons"],
                                     })
        return report

    def recheck_report(self, report_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, rules.RECHECK_ROLES)
        actor = require_text(actor, "actor", 100)
        expected = _expected_version(payload)
        before = self.repository.get_report(report_id)
        report = self.repository.recheck_report(
            report_id, expected, actor,
            lambda c, visas, reports: rules.decide_report(
                c, visas, reports, before["period"], before["quantity"],
                before["visa_no"]))
        self.repository.append_audit("report_recheck", rules.ENTITY_REPORT,
                                     report_id, actor, {
                                         "previous_reasons": before["hold_reasons"],
                                         "status": report["status"],
                                         "hold_reasons": report["hold_reasons"],
                                     })
        return report

    # ---- 支付 ----
    def pay_plan(self, plan_id: int, payload: Dict[str, Any], actor: str,
                 role: str) -> Dict[str, Any]:
        ensure_role(role, rules.PAY_ROLES)
        actor = require_text(actor, "actor", 100)
        expected = _expected_version(payload)
        note = _optional_text(payload, "note", 500) or ""
        plan, record = self.repository.pay_plan(plan_id, expected, note, actor)
        self.repository.append_audit("plan_pay", rules.ENTITY_PLAN, plan_id, actor, {
            "contract_id": plan["contract_id"], "period": plan["period"],
            "amount": plan["amount"], "record_id": record["id"],
        })
        return {"plan": plan, "record": record}

    # ---- 查询 ----
    def list_reports(self, role: str, contract_id: Optional[int] = None,
                     status: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, rules.SETTLEMENT_VIEW_ROLES)
        if status is not None and status not in rules.REPORT_STATUSES:
            raise ValidationError("status必须是pending或approved")
        return self.repository.list_reports(contract_id, status)

    def list_plans(self, role: str, contract_id: Optional[int] = None) -> List[Dict[str, Any]]:
        ensure_role(role, rules.SETTLEMENT_VIEW_ROLES)
        return self.repository.list_plans(contract_id)

    def list_payments(self, role: str, contract_id: Optional[int] = None) -> List[Dict[str, Any]]:
        ensure_role(role, rules.SETTLEMENT_VIEW_ROLES)
        return self.repository.list_payment_records(contract_id)

    def summary(self, role: str, contract_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """列表展示待确认、已核准与累计金额。"""
        ensure_role(role, rules.SETTLEMENT_VIEW_ROLES)
        if contract_id is not None:
            contracts = [self.repository.get_contract(contract_id)]
        else:
            contracts = self.repository.list_contracts()
        result = []
        for contract in contracts:
            cid = contract["id"]
            visas = self.repository.list_visas(cid)
            reports = self.repository.list_reports(cid)
            plans = self.repository.list_plans(cid)
            records = self.repository.list_payment_records(cid)
            pending = [r for r in reports if r["status"] == rules.REPORT_PENDING]
            approved = [r for r in reports if r["status"] == rules.REPORT_APPROVED]
            paid_amount = rules.round_money(sum(r["amount"] for r in records))
            unpaid_amount = rules.round_money(
                sum(p["amount"] for p in plans if p["status"] == rules.PLAN_UNPAID))
            result.append({
                "contract": contract,
                "visas": visas,
                "pending_reports": pending,
                "approved_reports": approved,
                "totals": {
                    "contract_quantity": contract["total_quantity"],
                    "visa_quantity": round(sum(v["quantity_delta"] for v in visas), 6),
                    "effective_quantity": round(rules.effective_quantity(contract, visas), 6),
                    "cumulative_quantity": round(rules.approved_quantity(reports), 6),
                    "planned_amount": rules.round_money(paid_amount + unpaid_amount),
                    "paid_amount": paid_amount,
                    "unpaid_amount": unpaid_amount,
                    "pending_count": len(pending),
                    "approved_count": len(approved),
                },
            })
        return result
