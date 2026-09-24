"""部分履约与资源释放：支付撤销、链上失败、物流退回、地址变更审批与补救案件。"""

import unittest

from src.service import FulfillmentError

from support import FulfillmentTestCase


class PartialFulfillmentTest(FulfillmentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.register_edition("ed-1", total_supply=5, physical_quota=1)

    def test_payment_revoked_before_ownership_releases_serial(self) -> None:
        svc = self.service
        hold, pay = self.paid_order("buyer-a", "pay-1")
        serial = hold["serial_no"]
        result = svc.revoke_payment(pay["order_id"], "买家主动退款")
        self.assertEqual(result["status"], "RELEASED")
        self.assertEqual(svc.serial_status("ed-1", serial), "AVAILABLE")
        # 释放后序号可被其他买家重新购买
        hold2 = svc.hold_serial("ed-1", "buyer-b", serial_no=serial)
        pay2 = svc.confirm_payment(hold2["reservation_id"], "pay-2", 12800)
        self.assertEqual(pay2["status"], "CONFIRMED")

    def test_physical_earmark_released_with_order(self) -> None:
        svc = self.service
        _, pay_a = self.paid_order("buyer-a", "pay-a", wants_physical=True, address="地址甲")
        hold_b = svc.hold_serial("ed-1", "buyer-b")
        # 实体额度只有 1，被甲占用
        with self.assertRaises(FulfillmentError) as ctx:
            svc.confirm_payment(hold_b["reservation_id"], "pay-b", 12800,
                                wants_physical=True, address="地址乙")
        self.assertEqual(ctx.exception.kind, "QUOTA_EXHAUSTED")
        # 甲撤销支付后额度释放，乙可重新以同一回执付款（此前未成交，不构成重放）
        svc.revoke_payment(pay_a["order_id"], "风控撤单")
        self.assertEqual(svc.physical_committed("ed-1"), 0)
        pay_b = svc.confirm_payment(hold_b["reservation_id"], "pay-b", 12800,
                                    wants_physical=True, address="地址乙")
        self.assertEqual(pay_b["status"], "CONFIRMED")
        self.assertTrue(pay_b["physical_earmarked"])

    def test_registration_failure_releases_resources(self) -> None:
        svc = self.service
        hold, pay = self.paid_order("buyer-a", "pay-1", wants_physical=True, address="地址甲")
        svc.approve_realname(pay["order_id"], reviewer="ops-1")
        svc.submit_registration(pay["order_id"])
        svc.fail_registration(pay["order_id"], "链上 Gas 不足")
        self.assertEqual(svc.serial_status("ed-1", hold["serial_no"]), "AVAILABLE")
        self.assertEqual(svc.physical_committed("ed-1"), 0)
        order = svc.order_state(pay["order_id"])
        self.assertEqual(order["status"], "RELEASED")
        self.assertEqual(order["registration"], "FAILED")

    def test_realname_rejection_releases_resources(self) -> None:
        svc = self.service
        hold, pay = self.paid_order("buyer-a", "pay-1")
        svc.reject_realname(pay["order_id"], "实名材料不清晰")
        self.assertEqual(svc.serial_status("ed-1", hold["serial_no"]), "AVAILABLE")
        self.assertEqual(svc.order_state(pay["order_id"])["status"], "RELEASED")

    def test_logistics_return_keeps_ownership_and_opens_case(self) -> None:
        svc = self.service
        hold, pay = self.owned_order("buyer-a", "pay-1", wants_physical=True, address="地址甲")
        serial = hold["serial_no"]
        shipment = svc.trace_serial("ed-1", serial)["shipments"][0]
        svc.mark_outbound(shipment["shipment_id"])
        svc.mark_returned(shipment["shipment_id"], "地址无人签收")
        processed = svc.process_returns()
        self.assertEqual(len(processed), 1)
        # 数字归属已登记，不能回滚
        self.assertEqual(svc.serial_status("ed-1", serial), "OWNED")
        # 实体额度回补，补救案件打开
        self.assertEqual(svc.physical_committed("ed-1"), 0)
        trace = svc.trace_serial("ed-1", serial)
        self.assertEqual(trace["shipments"][0]["status"], "RESTOCKED")
        self.assertEqual(trace["remediation_cases"][0]["reason"], "LOGISTICS_RETURNED")
        # 办结补救案件
        case_id = trace["remediation_cases"][0]["case_id"]
        svc.resolve_case(case_id, "重新核实收件人后补发")
        self.assertEqual(svc.case_state(case_id)["status"], "RESOLVED")
        types = {e["event_type"] for e in svc.trace_serial("ed-1", serial)["events"]}
        self.assertIn("ORDER_REMEDIED", types)

    def test_payment_revoked_after_ownership_goes_to_remediation(self) -> None:
        svc = self.service
        hold, pay = self.owned_order("buyer-a", "pay-1", wants_physical=True, address="地址甲")
        serial = hold["serial_no"]
        result = svc.revoke_payment(pay["order_id"], "支付渠道事后撤单")
        self.assertEqual(result["status"], "REMEDIATION")
        # 数字归属不回滚，未出库发运取消，案件打开
        self.assertEqual(svc.serial_status("ed-1", serial), "OWNED")
        trace = svc.trace_serial("ed-1", serial)
        self.assertEqual(trace["shipments"][0]["status"], "CANCELLED")
        self.assertEqual(trace["remediation_cases"][0]["reason"], "PAYMENT_REVOKED_AFTER_OWNERSHIP")
        self.assertEqual(svc.physical_committed("ed-1"), 0)

    def test_registered_ownership_cannot_rollback(self) -> None:
        svc = self.service
        _, pay = self.owned_order("buyer-a", "pay-1")
        with self.assertRaises(FulfillmentError) as ctx:
            svc.fail_registration(pay["order_id"], "迟到的失败回执")
        self.assertEqual(ctx.exception.kind, "INVALID_STATE")
        with self.assertRaises(FulfillmentError) as ctx:
            svc.reject_realname(pay["order_id"], "迟到的驳回")
        self.assertEqual(ctx.exception.kind, "INVALID_STATE")
        self.assertEqual(svc.order_state(pay["order_id"])["status"], "OWNERSHIP_CONFIRMED")

    def test_address_change_requires_approval_before_outbound(self) -> None:
        svc = self.service
        hold, _ = self.owned_order("buyer-a", "pay-1", wants_physical=True, address="地址甲")
        shipment_id = svc.trace_serial("ed-1", hold["serial_no"])["shipments"][0]["shipment_id"]
        svc.request_address_change(shipment_id, "地址乙")
        # 待审批期间禁止出库
        with self.assertRaises(FulfillmentError) as ctx:
            svc.mark_outbound(shipment_id)
        self.assertEqual(ctx.exception.kind, "INVALID_STATE")
        # 未经审批地址不生效
        self.assertEqual(svc.shipment_state(shipment_id)["address"], "地址甲")
        svc.reject_address_change(shipment_id, "买家取消变更")
        svc.mark_outbound(shipment_id)
        # 出库后变更不予受理
        with self.assertRaises(FulfillmentError) as ctx:
            svc.request_address_change(shipment_id, "地址丙")
        self.assertEqual(ctx.exception.kind, "INVALID_STATE")

    def test_address_change_approved_applies_new_address(self) -> None:
        svc = self.service
        hold, _ = self.owned_order("buyer-a", "pay-1", wants_physical=True, address="地址甲")
        shipment_id = svc.trace_serial("ed-1", hold["serial_no"])["shipments"][0]["shipment_id"]
        svc.request_address_change(shipment_id, "地址乙")
        svc.approve_address_change(shipment_id, approver="ops-2")
        self.assertEqual(svc.shipment_state(shipment_id)["address"], "地址乙")
        svc.mark_outbound(shipment_id)
        self.assertEqual(svc.shipment_state(shipment_id)["status"], "OUTBOUND")


if __name__ == "__main__":
    unittest.main()
