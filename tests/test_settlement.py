import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.settlement_rules import (REASON_DUPLICATE_PERIOD, REASON_MISSING_VISA,
                                  REASON_OVER_CONTRACT)
from src.settlement_service import SettlementService


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = SettlementService(self.repo)
        self.contract_payload = {
            "contract_no": "HT-2026-01", "title": "教学楼加固",
            "contractor": "某加固公司", "total_quantity": 100, "unit_price": 50,
        }

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def submit(self, period, quantity, **extra):
        payload = {"contract_id": self.contract_id, "period": period,
                   "quantity": quantity}
        payload.update(extra)
        return self.service.submit(payload, "施工员", "contractor")

    def create_contract(self):
        first = self.service.submit(
            {"contract": self.contract_payload, "period": "2026-08",
             "quantity": 40, "remark": "首期"}, "施工员", "contractor")
        self.contract_id = first["contract_id"]
        return first

    def test_approved_writes_payment_plan_and_cumulative(self):
        first = self.create_contract()
        self.assertEqual(first["status"], "approved")
        self.assertEqual(first["payment_plan"]["amount"], 2000.0)
        second = self.submit("2026-09", 30, remark="第二期")
        self.assertEqual(second["status"], "approved")
        view = self.service.list_settlements(self.contract_id, "settlement_admin")
        self.assertEqual(len(view["approved"]), 2)
        self.assertEqual(view["pending"], [])
        self.assertEqual(view["cumulative"]["approved_quantity"], 70.0)
        self.assertEqual(view["cumulative"]["planned_amount"], 3500.0)
        self.assertEqual(view["cumulative"]["unpaid_amount"], 3500.0)
        self.assertEqual(view["cumulative"]["paid_amount"], 0.0)

    def test_over_contract_missing_visa_and_duplicate_hold_pending(self):
        self.create_contract()
        over = self.submit("2026-09", 70)
        self.assertEqual(over["status"], "pending")
        self.assertIn(REASON_OVER_CONTRACT, over["reasons"])
        self.assertIn(REASON_MISSING_VISA, over["reasons"])
        with_visa = self.submit("2026-10", 70, visa_ref="QZ-1")
        self.assertEqual(with_visa["status"], "pending")
        self.assertIn(REASON_OVER_CONTRACT, with_visa["reasons"])
        self.assertNotIn(REASON_MISSING_VISA, with_visa["reasons"])
        dup = self.submit("2026-09", 5)
        self.assertEqual(dup["status"], "pending")
        self.assertIn(REASON_DUPLICATE_PERIOD, dup["reasons"])
        view = self.service.list_settlements(self.contract_id, "viewer")
        self.assertEqual(len(view["pending"]), 3)
        self.assertEqual(view["cumulative"]["planned_amount"], 2000.0)

    def test_correction_recalculates_unpaid_and_keeps_paid_archive(self):
        first = self.create_contract()
        paid_plan_id = first["payment_plan"]["id"]
        self.service.pay_plan(paid_plan_id, "出纳", "settlement_admin")
        second = self.submit("2026-09", 30)
        unpaid_plan_id = second["payment_plan"]["id"]
        contract = self.service.get_contract(self.contract_id, "viewer")
        updated = self.service.correct_contract(self.contract_id, {
            "total_quantity": 200, "unit_price": 60,
            "expected_version": contract["version"], "reason": "合同量更正",
        }, "预算科长", "settlement_admin")
        self.assertEqual(updated["total_quantity"], 200.0)
        self.assertEqual(updated["recalculated_plan_ids"], [unpaid_plan_id])
        view = self.service.list_settlements(self.contract_id, "settlement_admin")
        plans = {p["id"]: p for p in view["payment_plans"]}
        self.assertEqual(plans[unpaid_plan_id]["amount"], 1800.0)
        self.assertEqual(plans[paid_plan_id]["amount"], 2000.0)
        self.assertEqual(plans[paid_plan_id]["status"], "paid")
        self.assertEqual(view["payments"][0]["amount"], 2000.0)
        self.assertEqual(view["cumulative"]["paid_amount"], 2000.0)
        self.assertEqual(view["cumulative"]["unpaid_amount"], 1800.0)
        with self.assertRaises(ConflictError):
            self.service.pay_plan(paid_plan_id, "出纳", "settlement_admin")

    def test_pending_confirmed_after_visa_correction(self):
        self.create_contract()
        held = self.submit("2026-09", 70, visa_ref="QZ-1")
        self.assertEqual(held["status"], "pending")
        contract = self.service.get_contract(self.contract_id, "viewer")
        self.service.correct_contract(self.contract_id, {
            "visa_quantity": 30, "expected_version": contract["version"],
            "reason": "签证更正",
        }, "预算科长", "settlement_admin")
        confirmed = self.service.confirm(held["id"], "预算科长", "settlement_admin")
        self.assertEqual(confirmed["status"], "approved")
        self.assertEqual(confirmed["payment_plan"]["amount"], 3500.0)
        again = self.service.confirm(
            self.submit("2026-10", 200)["id"], "预算科长", "settlement_admin")
        self.assertEqual(again["status"], "pending")
        self.assertIn(REASON_OVER_CONTRACT, again["reasons"])

    def test_budget_officer_sees_remarks_only(self):
        self.create_contract()
        self.submit("2026-09", 10, remark="二层梁柱注胶")
        view = self.service.list_settlements(self.contract_id, "budget_officer")
        self.assertNotIn("cumulative", view)
        self.assertNotIn("payment_plans", view)
        for row in view["submissions"]:
            self.assertEqual(set(row), {"id", "period", "status", "remark"})

    def test_permissions_and_validation(self):
        self.create_contract()
        with self.assertRaises(PermissionDenied):
            self.service.submit({"contract_id": self.contract_id, "period": "2026-09",
                                 "quantity": 1}, "attacker", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.correct_contract(self.contract_id, {
                "total_quantity": 1, "expected_version": 1,
            }, "attacker", "contractor")
        with self.assertRaises(PermissionDenied):
            self.service.list_settlements(self.contract_id, "contractor")
        held = self.submit("2026-09", 70)
        with self.assertRaises(PermissionDenied):
            self.service.confirm(held["id"], "施工员", "contractor")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
