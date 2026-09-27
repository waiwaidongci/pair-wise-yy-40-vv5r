from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

TITLE = "加固施工结算"
ENTITY_CONTRACT = "施工合同"
ENTITY_VISA = "签证"
ENTITY_REPORT = "报量单"
ENTITY_PLAN = "付款计划"

REPORT_PENDING = "pending"
REPORT_APPROVED = "approved"
REPORT_STATUSES = [REPORT_PENDING, REPORT_APPROVED]

PLAN_UNPAID = "unpaid"
PLAN_PAID = "paid"

HOLD_DUPLICATE_PERIOD = "同一期重复提交"
HOLD_MISSING_VISA = "缺签证"
HOLD_EXCEED_CONTRACT = "累计报量超过合同量"

CONTRACT_WRITE_ROLES = {"contractor", "budget_officer"}
REPORT_SUBMIT_ROLES = {"contractor"}
VISA_WRITE_ROLES = {"budget_officer"}
CORRECT_ROLES = {"budget_officer"}
RECHECK_ROLES = {"budget_officer"}
PAY_ROLES = {"finance"}
SETTLEMENT_VIEW_ROLES = {"contractor", "budget_officer", "finance", "viewer"}

EPSILON = 1e-6


def round_money(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def find_visa(visas: List[Dict[str, Any]], visa_no: Optional[str]) -> Optional[Dict[str, Any]]:
    if visa_no is None:
        return None
    for visa in visas:
        if visa["visa_no"] == visa_no:
            return visa
    return None


def effective_quantity(contract: Dict[str, Any], visas: List[Dict[str, Any]]) -> float:
    """合同量加签证增减量后的可报量上限。"""
    return contract["total_quantity"] + sum(v["quantity_delta"] for v in visas)


def approved_quantity(reports: List[Dict[str, Any]]) -> float:
    return sum(r["quantity"] for r in reports if r["status"] == REPORT_APPROVED)


def determine_submission(contract: Dict[str, Any], visas: List[Dict[str, Any]],
                         reports: List[Dict[str, Any]], period: str,
                         quantity: float, visa_no: Optional[str]) -> List[str]:
    """结算判定：返回待确认原因列表，为空表示核对通过。"""
    reasons = []
    if any(r["period"] == period and r["status"] in REPORT_STATUSES for r in reports):
        reasons.append(HOLD_DUPLICATE_PERIOD)
    if visa_no is not None and find_visa(visas, visa_no) is None:
        reasons.append(HOLD_MISSING_VISA)
    if approved_quantity(reports) + quantity - effective_quantity(contract, visas) > EPSILON:
        reasons.append(HOLD_EXCEED_CONTRACT)
    return reasons


def plan_amount(contract: Dict[str, Any], visa: Optional[Dict[str, Any]],
                quantity: float) -> float:
    amount = quantity * contract["unit_price"]
    if visa is not None:
        amount += visa["amount_delta"]
    return round_money(amount)


def decide_report(contract: Dict[str, Any], visas: List[Dict[str, Any]],
                  reports: List[Dict[str, Any]], period: str, quantity: float,
                  visa_no: Optional[str]) -> Tuple[str, List[str], Optional[float]]:
    """返回(状态, 待确认原因, 通过时的计划金额)。"""
    reasons = determine_submission(contract, visas, reports, period, quantity, visa_no)
    if reasons:
        return REPORT_PENDING, reasons, None
    visa = find_visa(visas, visa_no)
    return REPORT_APPROVED, [], plan_amount(contract, visa, quantity)


def recalc_plan_amount(contract: Dict[str, Any], visas: List[Dict[str, Any]],
                       report: Dict[str, Any]) -> float:
    """合同或签证更正后，按新值重算未付计划金额。"""
    visa = find_visa(visas, report.get("visa_no"))
    return plan_amount(contract, visa, report["quantity"])
