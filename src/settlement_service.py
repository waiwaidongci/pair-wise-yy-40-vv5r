from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, require_number, require_text)
from .repository import Repository
from .settlement_rules import (CONFIRM_ROLES, CORRECT_ROLES, ENTITY_CONTRACT,
                               ENTITY_PAYMENT, ENTITY_SETTLEMENT, PAY_ROLES,
                               REMARK_ONLY_ROLES, SUBMIT_ROLES, VIEW_ROLES,
                               determine_submission, plan_amount)


class SettlementService:
    """施工结算用例编排：提交报量、核对确认、更正重算、支付留档。"""

    def __init__(self, repository: Repository):
        self.repository = repository

    # ---- 提交报量 ----
    def submit(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        contract = self._resolve_contract(payload, actor)
        period = require_text(payload.get("period"), "period", 50)
        quantity = require_number(payload.get("quantity"), "quantity", 0.000001)
        remark = payload.get("remark")
        remark = require_text(remark, "remark") if remark is not None else ""
        visa_ref = payload.get("visa_ref")
        if visa_ref is not None:
            visa_ref = require_text(visa_ref, "visa_ref", 100)
        result = determine_submission(
            contract["total_quantity"], contract["visa_quantity"],
            self.repository.approved_quantity(contract["id"]),
            quantity, period, visa_ref,
            self.repository.submission_periods(contract["id"]))
        submission = self.repository.create_submission(
            contract["id"], period, quantity, remark, visa_ref,
            result["status"], result["reasons"], actor)
        self.repository.append_audit(
            "settlement.submit", ENTITY_SETTLEMENT, submission["id"], actor, {
                "contract_id": contract["id"], "period": period,
                "quantity": quantity, "status": result["status"],
                "reasons": result["reasons"],
            })
        plan = None
        if result["status"] == "approved":
            plan = self._approve(submission, contract, actor)
        return self._submission_view(submission, plan)

    def _resolve_contract(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        contract_id = payload.get("contract_id")
        if contract_id is not None:
            try:
                contract_id = int(contract_id)
            except (TypeError, ValueError):
                from .domain import ValidationError
                raise ValidationError("contract_id必须是整数")
            return self.repository.get_contract(contract_id)
        contract = payload.get("contract")
        if not isinstance(contract, dict):
            from .domain import ValidationError
            raise ValidationError("必须提供contract_id或contract")
        contract_no = require_text(contract.get("contract_no"), "contract_no", 100)
        title = require_text(contract.get("title"), "title", 200)
        contractor = require_text(contract.get("contractor"), "contractor", 200)
        total_quantity = require_number(
            contract.get("total_quantity"), "total_quantity", 0.000001)
        unit_price = require_number(contract.get("unit_price"), "unit_price", 0.000001)
        created = self.repository.create_contract(
            contract_no, title, contractor, total_quantity, unit_price, actor)
        self.repository.append_audit(
            "contract.create", ENTITY_CONTRACT, created["id"], actor, {
                "contract_no": contract_no, "contractor": contractor,
                "total_quantity": total_quantity, "unit_price": unit_price,
            })
        return created

    def _approve(self, submission: Dict[str, Any], contract: Dict[str, Any],
                 actor: str) -> Dict[str, Any]:
        plan = self.repository.create_payment_plan(
            contract["id"], submission["id"], submission["period"],
            submission["quantity"],
            plan_amount(submission["quantity"], contract["unit_price"]))
        self.repository.append_audit(
            "settlement.approve", ENTITY_SETTLEMENT, submission["id"], actor, {
                "contract_id": contract["id"], "plan_id": plan["id"],
                "amount": plan["amount"],
            })
        return plan

    # ---- 核对确认 ----
    def confirm(self, submission_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        submission = self.repository.get_submission(submission_id)
        if submission["status"] != "pending":
            raise ConflictError("仅待确认的报量可以核对")
        contract = self.repository.get_contract(submission["contract_id"])
        result = determine_submission(
            contract["total_quantity"], contract["visa_quantity"],
            self.repository.approved_quantity(contract["id"]),
            submission["quantity"], submission["period"], submission["visa_ref"],
            self.repository.submission_periods(contract["id"], exclude_id=submission_id))
        plan = None
        if result["status"] == "approved":
            submission = self.repository.update_submission_status(
                submission_id, "approved", [])
            plan = self._approve(submission, contract, actor)
        else:
            submission = self.repository.update_submission_status(
                submission_id, "pending", result["reasons"])
            self.repository.append_audit(
                "settlement.hold", ENTITY_SETTLEMENT, submission_id, actor, {
                    "contract_id": contract["id"], "reasons": result["reasons"],
                })
        return self._submission_view(submission, plan)

    # ---- 合同量/签证更正：未付计划按新值重算，已付留档 ----
    def correct_contract(self, contract_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CORRECT_ROLES)
        actor = require_text(actor, "actor", 100)
        contract = self.repository.get_contract(contract_id)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        total_quantity = require_number(
            payload.get("total_quantity", contract["total_quantity"]),
            "total_quantity", 0.000001)
        visa_quantity = require_number(
            payload.get("visa_quantity", contract["visa_quantity"]), "visa_quantity")
        unit_price = require_number(
            payload.get("unit_price", contract["unit_price"]), "unit_price", 0.000001)
        reason = payload.get("reason")
        reason = require_text(reason, "reason") if reason is not None else ""
        updated = self.repository.update_contract(
            contract_id, total_quantity, visa_quantity, unit_price,
            expected_version, actor)
        recalculated = self.repository.recalc_unpaid_plans(contract_id, unit_price)
        self.repository.append_audit(
            "contract.correct", ENTITY_CONTRACT, contract_id, actor, {
                "old": {"total_quantity": contract["total_quantity"],
                        "visa_quantity": contract["visa_quantity"],
                        "unit_price": contract["unit_price"]},
                "new": {"total_quantity": total_quantity,
                        "visa_quantity": visa_quantity, "unit_price": unit_price},
                "reason": reason, "recalculated_plan_ids": recalculated,
            })
        result = dict(updated)
        result["recalculated_plan_ids"] = recalculated
        return result

    # ---- 支付留档 ----
    def pay_plan(self, plan_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, PAY_ROLES)
        actor = require_text(actor, "actor", 100)
        plan = self.repository.get_payment_plan(plan_id)
        if plan["status"] != "unpaid":
            raise ConflictError("该计划已支付")
        paid = self.repository.mark_plan_paid(plan_id)
        payment = self.repository.create_payment(
            plan_id, plan["contract_id"], plan["amount"], actor)
        self.repository.append_audit(
            "payment.pay", ENTITY_PAYMENT, plan_id, actor, {
                "contract_id": plan["contract_id"], "amount": plan["amount"],
                "payment_id": payment["id"],
            })
        return {"plan": paid, "payment": payment}

    # ---- 列表：待确认、已核准、累计金额 ----
    def list_settlements(self, contract_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        contract = self.repository.get_contract(contract_id)
        submissions = self.repository.list_submissions(contract_id)
        if role in REMARK_ONLY_ROLES:
            return {
                "contract_id": contract["id"],
                "submissions": [
                    {"id": s["id"], "period": s["period"], "status": s["status"],
                     "remark": s["remark"]}
                    for s in submissions
                ],
            }
        plans = self.repository.list_payment_plans(contract_id)
        payments = self.repository.list_payments(contract_id)
        cumulative = {
            "approved_quantity": round(sum(
                s["quantity"] for s in submissions if s["status"] == "approved"), 2),
            "planned_amount": round(sum(p["amount"] for p in plans), 2),
            "paid_amount": round(sum(p["amount"] for p in payments), 2),
            "unpaid_amount": round(sum(
                p["amount"] for p in plans if p["status"] == "unpaid"), 2),
        }
        return {
            "contract": contract,
            "pending": [self._submission_view(s) for s in submissions
                        if s["status"] == "pending"],
            "approved": [self._submission_view(s) for s in submissions
                         if s["status"] == "approved"],
            "payment_plans": plans,
            "payments": payments,
            "cumulative": cumulative,
        }

    def get_contract(self, contract_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, VIEW_ROLES)
        return self.repository.get_contract(contract_id)

    def list_contracts(self, role: str) -> List[Dict[str, Any]]:
        ensure_role(role, VIEW_ROLES)
        return self.repository.list_contracts()

    @staticmethod
    def _submission_view(submission: Dict[str, Any],
                         plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = dict(submission)
        try:
            result["reasons"] = json.loads(result.get("reasons") or "[]")
        except (TypeError, ValueError):
            result["reasons"] = []
        if plan is not None:
            result["payment_plan"] = plan
        return result
