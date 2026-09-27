import tempfile
import unittest
from pathlib import Path

from src import settlement_rules as rules
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.settlement_repository import SettlementRepository
from src.settlement_service import SettlementService


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SettlementRepository(str(Path(self.tmp.name) / "test.db"))
        self.service = SettlementService(self.repo)
        self.contract = self.service.create_contract(
            {"contract_no": "HT-2026-001", "contractor": "加固一队",
             "total_quantity": 1000, "unit_price": 50}, "zhang", "contractor")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def submit(self, period, quantity, visa_no=None):
        payload = {"contract_no": "HT-2026-001", "period": period,
                   "quantity": quantity, "remark": "节点报量"}
        if visa_no is not None:
            payload["visa_no"] = visa_no
        return self.service.submit_report(payload, "wang", "contractor")

    def test_approved_report_writes_payment_plan(self):
        report = self.submit("2026-08", 400)
        self.assertEqual(report["status"], rules.REPORT_APPROVED)
        self.assertEqual(report["hold_reasons"], [])
        plans = self.service.list_plans("viewer", self.contract["id"])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["status"], rules.PLAN_UNPAID)
        self.assertEqual(plans[0]["amount"], 20000)
        summary = self.service.summary("budget_officer", self.contract["id"])
        totals = summary[0]["totals"]
        self.assertEqual(totals["cumulative_quantity"], 400)
        self.assertEqual(totals["planned_amount"], 20000)
        self.assertEqual(totals["paid_amount"], 0)
        self.assertEqual(totals["unpaid_amount"], 20000)

    def test_exceed_holds_then_visa_recheck_approves(self):
        self.submit("2026-08", 800)
        held = self.submit("2026-09", 300, visa_no="QZ-1")
        self.assertEqual(held["status"], rules.REPORT_PENDING)
        self.assertEqual(held["hold_reasons"],
                         [rules.HOLD_MISSING_VISA, rules.HOLD_EXCEED_CONTRACT])
        self.assertEqual(self.service.list_plans("viewer", self.contract["id"])[0]["period"],
                         "2026-08")
        again = self.service.recheck_report(
            held["id"], {"expected_version": held["version"]}, "qian", "budget_officer")
        self.assertEqual(again["status"], rules.REPORT_PENDING)
        self.service.add_visa(self.contract["id"], {"visa_no": "QZ-1", "quantity_delta": 300,
                                                    "amount_delta": 9000,
                                                    "reason": "设计变更增加加固量"},
                              "qian", "budget_officer")
        approved = self.service.recheck_report(
            held["id"], {"expected_version": again["version"]}, "qian", "budget_officer")
        self.assertEqual(approved["status"], rules.REPORT_APPROVED)
        self.assertEqual(approved["hold_reasons"], [])
        plans = self.service.list_plans("viewer", self.contract["id"])
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[1]["amount"], 300 * 50 + 9000)

    def test_missing_visa_holds_until_visa_registered(self):
        held = self.submit("2026-08", 100, visa_no="QZ-9")
        self.assertEqual(held["status"], rules.REPORT_PENDING)
        self.assertEqual(held["hold_reasons"], [rules.HOLD_MISSING_VISA])
        self.service.add_visa(self.contract["id"], {"visa_no": "QZ-9", "quantity_delta": 0,
                                                    "amount_delta": 500,
                                                    "reason": "现场零星签证"},
                              "qian", "budget_officer")
        approved = self.service.recheck_report(
            held["id"], {"expected_version": held["version"]}, "qian", "budget_officer")
        self.assertEqual(approved["status"], rules.REPORT_APPROVED)
        plan = self.service.list_plans("viewer", self.contract["id"])[0]
        self.assertEqual(plan["amount"], 100 * 50 + 500)

    def test_duplicate_period_holds(self):
        self.submit("2026-08", 100)
        dup = self.submit("2026-08", 50)
        self.assertEqual(dup["status"], rules.REPORT_PENDING)
        self.assertEqual(dup["hold_reasons"], [rules.HOLD_DUPLICATE_PERIOD])
        pending = self.service.list_reports("viewer", self.contract["id"], "pending")
        self.assertEqual([r["id"] for r in pending], [dup["id"]])

    def test_first_report_not_blocked_by_later_duplicate(self):
        first = self.submit("2026-08", 900)
        self.assertEqual(first["status"], rules.REPORT_APPROVED)
        held = self.submit("2026-09", 200, visa_no="QZ-1")
        dup = self.submit("2026-09", 50)
        self.assertEqual(dup["hold_reasons"], [rules.HOLD_DUPLICATE_PERIOD])
        self.service.add_visa(self.contract["id"], {"visa_no": "QZ-1",
                                                    "quantity_delta": 200,
                                                    "amount_delta": 0, "reason": "变更"},
                              "qian", "budget_officer")
        approved = self.service.recheck_report(
            held["id"], {"expected_version": held["version"]}, "qian", "budget_officer")
        self.assertEqual(approved["status"], rules.REPORT_APPROVED)
        still_dup = self.service.recheck_report(
            dup["id"], {"expected_version": dup["version"]}, "qian", "budget_officer")
        self.assertEqual(still_dup["status"], rules.REPORT_PENDING)
        self.assertEqual(still_dup["hold_reasons"], [rules.HOLD_DUPLICATE_PERIOD])

    def test_contract_correction_recalculates_unpaid_only(self):
        self.submit("2026-08", 400)
        self.submit("2026-09", 400)
        plans = self.service.list_plans("viewer", self.contract["id"])
        paid = self.service.pay_plan(plans[0]["id"],
                                     {"expected_version": plans[0]["version"],
                                      "note": "按节点支付"}, "cai", "finance")
        self.assertEqual(paid["record"]["amount"], 20000)
        result = self.service.correct_contract(
            self.contract["id"], {"unit_price": 55, "expected_version": 1},
            "qian", "budget_officer")
        self.assertEqual(len(result["recalculated_plans"]), 1)
        self.assertEqual(result["recalculated_plans"][0]["new_amount"], 400 * 55)
        plans = self.service.list_plans("viewer", self.contract["id"])
        self.assertEqual(plans[0]["amount"], 20000)
        self.assertEqual(plans[0]["status"], rules.PLAN_PAID)
        self.assertEqual(plans[1]["amount"], 22000)
        records = self.service.list_payments("viewer", self.contract["id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["amount"], 20000)
        totals = self.service.summary("viewer", self.contract["id"])[0]["totals"]
        self.assertEqual(totals["paid_amount"], 20000)
        self.assertEqual(totals["unpaid_amount"], 22000)

    def test_visa_correction_recalculates_referencing_plan(self):
        visa = self.service.add_visa(
            self.contract["id"], {"visa_no": "QZ-1", "quantity_delta": 100,
                                  "amount_delta": 1000, "reason": "变更"},
            "qian", "budget_officer")
        self.submit("2026-08", 100, visa_no="QZ-1")
        plan = self.service.list_plans("viewer", self.contract["id"])[0]
        self.assertEqual(plan["amount"], 100 * 50 + 1000)
        result = self.service.correct_visa(
            visa["id"], {"amount_delta": 2000, "expected_version": visa["version"]},
            "qian", "budget_officer")
        self.assertEqual(result["recalculated_plans"][0]["new_amount"], 100 * 50 + 2000)
        plan = self.service.list_plans("viewer", self.contract["id"])[0]
        self.assertEqual(plan["amount"], 7000)

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_report({"contract_no": "HT-2026-001", "period": "2026-08",
                                        "quantity": 1}, "eve", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create_contract({"contract_no": "X", "contractor": "Y",
                                          "total_quantity": 1, "unit_price": 1},
                                         "eve", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.add_visa(self.contract["id"], {"visa_no": "Q", "reason": "r"},
                                  "wang", "contractor")
        with self.assertRaises(PermissionDenied):
            self.service.correct_contract(self.contract["id"],
                                          {"unit_price": 60, "expected_version": 1},
                                          "wang", "contractor")
        report = self.submit("2026-08", 100)
        plan = self.service.list_plans("viewer", self.contract["id"])[0]
        with self.assertRaises(PermissionDenied):
            self.service.pay_plan(plan["id"], {"expected_version": 1}, "qian", "budget_officer")
        with self.assertRaises(PermissionDenied):
            self.service.recheck_report(report["id"], {"expected_version": 1},
                                        "cai", "finance")
        with self.assertRaises(PermissionDenied):
            self.service.summary("stranger")

    def test_version_and_state_conflicts(self):
        with self.assertRaises(ConflictError):
            self.service.correct_contract(self.contract["id"],
                                          {"unit_price": 60, "expected_version": 99},
                                          "qian", "budget_officer")
        report = self.submit("2026-08", 100)
        with self.assertRaises(ConflictError):
            self.service.recheck_report(report["id"], {"expected_version": 1},
                                        "qian", "budget_officer")
        plan = self.service.list_plans("viewer", self.contract["id"])[0]
        with self.assertRaises(ConflictError):
            self.service.pay_plan(plan["id"], {"expected_version": 99}, "cai", "finance")
        self.service.pay_plan(plan["id"], {"expected_version": plan["version"]},
                              "cai", "finance")
        with self.assertRaises(ConflictError):
            self.service.pay_plan(plan["id"], {"expected_version": plan["version"] + 1},
                                  "cai", "finance")

    def test_validation_and_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.submit_report({"contract_no": "NOPE", "period": "2026-08",
                                        "quantity": 1}, "wang", "contractor")
        with self.assertRaises(ValidationError):
            self.service.submit_report({"contract_no": "HT-2026-001", "period": "2026-08",
                                        "quantity": 0}, "wang", "contractor")
        with self.assertRaises(ValidationError):
            self.service.correct_contract(self.contract["id"], {"expected_version": 1},
                                          "qian", "budget_officer")
        with self.assertRaises(ValidationError):
            self.service.list_reports("viewer", self.contract["id"], "unknown")
        with self.assertRaises(ConflictError):
            self.service.create_contract({"contract_no": "HT-2026-001", "contractor": "乙",
                                          "total_quantity": 1, "unit_price": 1},
                                         "qian", "budget_officer")

    def test_summary_lists_pending_approved_and_amounts(self):
        self.submit("2026-08", 400)
        self.submit("2026-09", 700)
        self.submit("2026-09", 10)
        summary = self.service.summary("viewer", self.contract["id"])[0]
        self.assertEqual(summary["totals"]["approved_count"], 1)
        self.assertEqual(summary["totals"]["pending_count"], 2)
        reasons = {tuple(r["hold_reasons"]) for r in summary["pending_reports"]}
        self.assertEqual(reasons, {(rules.HOLD_EXCEED_CONTRACT,),
                                   (rules.HOLD_DUPLICATE_PERIOD,)})
        self.assertEqual(summary["totals"]["cumulative_quantity"], 400)
        self.assertEqual(summary["totals"]["planned_amount"], 20000)

    def test_audit_chain_covers_settlement_events(self):
        self.submit("2026-08", 800)
        held = self.submit("2026-09", 300)
        self.service.add_visa(self.contract["id"], {"visa_no": "QZ-1",
                                                    "quantity_delta": 300,
                                                    "amount_delta": 0, "reason": "变更"},
                              "qian", "budget_officer")
        self.service.recheck_report(held["id"], {"expected_version": held["version"]},
                                    "qian", "budget_officer")
        plan = self.service.list_plans("viewer", self.contract["id"])[0]
        self.service.pay_plan(plan["id"], {"expected_version": plan["version"]},
                              "cai", "finance")
        actions = [e["action"] for e in self.repo.list_audit()]
        for expected in ("contract_create", "report_submit", "visa_create",
                         "report_recheck", "plan_pay"):
            self.assertIn(expected, actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
