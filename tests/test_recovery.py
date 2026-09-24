"""重启恢复：状态重建后继续过期释放、补登记与退件处理。"""

import unittest
from datetime import timedelta

from support import HOLD_TTL, FulfillmentTestCase


class RecoveryTest(FulfillmentTestCase):
    def test_restart_rebuilds_state(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5, physical_quota=1)
        hold, pay = self.paid_order("buyer-a", "pay-1", wants_physical=True, address="地址甲")
        svc.approve_realname(pay["order_id"], reviewer="ops-1")
        svc.submit_registration(pay["order_id"])
        self.restart()
        svc = self.service
        self.assertEqual(svc.serial_status("ed-1", hold["serial_no"]), "SOLD")
        order = svc.order_state(pay["order_id"])
        self.assertEqual(order["realname"], "APPROVED")
        self.assertEqual(order["registration"], "SUBMITTED")
        self.assertTrue(order["physical_earmarked"])
        self.assertEqual(svc.physical_committed("ed-1"), 1)
        # 重建后继续走通主链路
        svc.confirm_registration(pay["order_id"], "rcpt-1", "0xabc")
        self.assertEqual(svc.serial_status("ed-1", hold["serial_no"]), "OWNED")
        self.assertEqual(len(svc.trace_serial("ed-1", hold["serial_no"])["shipments"]), 1)

    def test_recover_continues_pending_work(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5, physical_quota=1)
        # 将在停机期间过期的预占
        expiring = svc.hold_serial("ed-1", "buyer-a")
        # 付款后等待补登记的订单
        _, pay = self.paid_order("buyer-b", "pay-1")
        svc.approve_realname(pay["order_id"], reviewer="ops-1")
        # 已归属且实体在途被退回的订单
        owned_hold, owned_pay = self.owned_order("buyer-c", "pay-2",
                                                 wants_physical=True, address="地址丙")
        shipment = svc.trace_serial("ed-1", owned_hold["serial_no"])["shipments"][0]
        svc.mark_outbound(shipment["shipment_id"])
        svc.mark_returned(shipment["shipment_id"], "收件人拒收")

        self.clock.advance(HOLD_TTL + timedelta(minutes=1))  # 停机期间预占过期
        self.restart()
        svc = self.service
        report = svc.recover()

        # 过期释放继续
        self.assertEqual(report["expired_holds"], [expiring["reservation_id"]])
        self.assertEqual(svc.serial_status("ed-1", expiring["serial_no"]), "AVAILABLE")
        # 补登记继续：待登记订单被重新提交
        self.assertEqual(report["registrations_retried"], [pay["order_id"]])
        self.assertEqual(svc.order_state(pay["order_id"])["registration"], "SUBMITTED")
        # 退件处理继续：实体回补，数字归属不回滚、转入补救
        self.assertEqual(len(report["returns_processed"]), 1)
        self.assertEqual(svc.serial_status("ed-1", owned_hold["serial_no"]), "OWNED")
        self.assertEqual(svc.physical_committed("ed-1"), 0)
        trace = svc.trace_serial("ed-1", owned_hold["serial_no"])
        self.assertEqual(trace["shipments"][0]["status"], "RESTOCKED")
        self.assertEqual(trace["remediation_cases"][0]["reason"], "LOGISTICS_RETURNED")
        self.assertEqual(trace["remediation_cases"][0]["status"], "OPEN")

    def test_recover_is_idempotent_when_nothing_pending(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5)
        self.owned_order("buyer-a", "pay-1")
        self.restart()
        svc = self.service
        before = len(svc.events)
        report = svc.recover()
        self.assertEqual(report, {"expired_holds": [], "registrations_retried": [],
                                  "returns_processed": []})
        self.assertEqual(len(svc.events), before)


if __name__ == "__main__":
    unittest.main()
