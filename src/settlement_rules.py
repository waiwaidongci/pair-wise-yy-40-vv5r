from __future__ import annotations

TITLE = '加固施工结算'
ENTITY_CONTRACT = '施工合同'
ENTITY_SETTLEMENT = '施工结算'
ENTITY_PAYMENT = '付款计划'

EPSILON = 1e-9
SUBMISSION_STATES = ['pending', 'approved']
PLAN_STATES = ['unpaid', 'paid']

REASON_OVER_CONTRACT = '累计报量超过合同量'
REASON_MISSING_VISA = '缺签证'
REASON_DUPLICATE_PERIOD = '同一期重复提交'

SUBMIT_ROLES = set(['contractor'])
CONFIRM_ROLES = set(['settlement_admin'])
CORRECT_ROLES = set(['settlement_admin'])
PAY_ROLES = set(['settlement_admin'])
VIEW_ROLES = set(['settlement_admin', 'budget_officer', 'viewer'])
REMARK_ONLY_ROLES = set(['budget_officer'])


def determine_submission(total_quantity, visa_quantity, approved_cumulative,
                         quantity, period, visa_ref, existing_periods):
    """结算判定：命中任一 stop 条件即停在待确认并给出原因。"""
    reasons = []
    if period in existing_periods:
        reasons.append(REASON_DUPLICATE_PERIOD)
    projected = approved_cumulative + quantity
    effective = total_quantity + visa_quantity
    if projected > effective + EPSILON:
        reasons.append(REASON_OVER_CONTRACT)
    if projected - total_quantity > visa_quantity + EPSILON and not visa_ref:
        reasons.append(REASON_MISSING_VISA)
    return {
        'status': 'pending' if reasons else 'approved',
        'reasons': reasons,
        'projected_quantity': projected,
    }


def plan_amount(quantity, unit_price):
    return round(float(quantity) * float(unit_price), 2)
