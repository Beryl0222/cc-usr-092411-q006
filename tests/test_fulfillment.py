"""主链路：版次登记 → 预占 → 付款 → 实名复核 → 链上登记 → 归属 → 发运。"""

import unittest

from src.service import FulfillmentError
from src.validator import validate_event

from support import FulfillmentTestCase


class FullChainTest(FulfillmentTestCase):
    def test_full_chain_and_trace(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=10, reserved_ranges=[(1, 2)], physical_quota=1)

        hold = svc.hold_serial("ed-1", buyer_id="buyer-a")
        self.assertEqual(hold["serial_no"], 3)  # 1、2 为保留范围
        self.assertEqual(svc.serial_status("ed-1", 3), "HELD")

        pay = svc.confirm_payment(hold["reservation_id"], "pay-1", 12800,
                                  wants_physical=True, address="上海市黄浦区中山东一路 1 号")
        self.assertEqual(pay["status"], "CONFIRMED")
        self.assertTrue(pay["physical_earmarked"])
        # 付款只锁定购买资格，尚未确认归属
        self.assertEqual(svc.serial_status("ed-1", 3), "SOLD")

        svc.approve_realname(pay["order_id"], reviewer="ops-1")
        self.assertEqual(svc.serial_status("ed-1", 3), "SOLD")  # 链上登记未确认仍非归属
        svc.submit_registration(pay["order_id"])
        reg = svc.confirm_registration(pay["order_id"], "rcpt-1", "0xabc")
        self.assertEqual(reg["ownership"], "OWNERSHIP_CONFIRMED")
        self.assertEqual(svc.serial_status("ed-1", 3), "OWNED")

        # 实体装裱按同一归属生成发运任务
        trace = svc.trace_serial("ed-1", 3)
        self.assertEqual(len(trace["shipments"]), 1)
        shipment = trace["shipments"][0]
        self.assertEqual(shipment["address"], "上海市黄浦区中山东一路 1 号")
        self.assertEqual(shipment["status"], "CREATED")

        # 出库前地址变更需审批
        svc.request_address_change(shipment["shipment_id"], "北京市朝阳区建国路 88 号")
        svc.approve_address_change(shipment["shipment_id"], approver="ops-2")
        svc.mark_outbound(shipment["shipment_id"])
        svc.mark_delivered(shipment["shipment_id"])

        # 从序号还原预占、付款、登记、发运依据
        trace = svc.trace_serial("ed-1", 3)
        self.assertEqual(trace["status"], "OWNED")
        self.assertEqual(trace["shipments"][0]["address"], "北京市朝阳区建国路 88 号")
        self.assertEqual(trace["shipments"][0]["status"], "DELIVERED")
        self.assertEqual(len(trace["reservations"]), 1)
        self.assertEqual(len(trace["orders"]), 1)
        self.assertEqual(trace["orders"][0]["tx_hash"], "0xabc")
        stages = {e["aggregate_type"] for e in trace["events"]}
        self.assertIn("serial_reservation", stages)
        self.assertIn("purchase_order", stages)
        self.assertIn("shipment", stages)
        types = {e["event_type"] for e in trace["events"]}
        for expected in ("SERIAL_HELD", "PAYMENT_CONFIRMED", "REALNAME_APPROVED",
                         "REGISTRATION_ACCEPTED", "OWNERSHIP_CONFIRMED", "PHYSICAL_DISPATCHED"):
            self.assertIn(expected, types)

        # 全链路事件均符合领域信封约定
        for event in svc.events:
            self.assertEqual(validate_event(event), [], event["event_id"])

    def test_registration_before_realname_also_confirms_ownership(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5)
        hold, pay = self.paid_order("buyer-a", "pay-1")
        svc.submit_registration(pay["order_id"])
        svc.confirm_registration(pay["order_id"], "rcpt-1", "0xabc")
        self.assertEqual(svc.serial_status("ed-1", hold["serial_no"]), "SOLD")
        result = svc.approve_realname(pay["order_id"], reviewer="ops-1")
        self.assertEqual(result["ownership"], "OWNERSHIP_CONFIRMED")

    def test_reserved_ranges_not_allocated(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5, reserved_ranges=[(1, 2), (5, 5)])
        self.assertEqual(svc.available_serials("ed-1"), [3, 4])
        self.assertEqual(svc.serial_status("ed-1", 1), "RESERVED")
        with self.assertRaises(FulfillmentError) as ctx:
            svc.hold_serial("ed-1", "buyer-a", serial_no=1)
        self.assertEqual(ctx.exception.kind, "SERIAL_RESERVED")
        with self.assertRaises(FulfillmentError) as ctx:
            svc.hold_serial("ed-1", "buyer-a", serial_no=99)
        self.assertEqual(ctx.exception.kind, "SERIAL_OUT_OF_RANGE")

    def test_payment_locks_eligibility_only(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5)
        hold, pay = self.paid_order("buyer-a", "pay-1")
        serial = hold["serial_no"]
        self.assertEqual(svc.serial_status("ed-1", serial), "SOLD")
        # 资格被锁定后，其他买家不能再预占同一序号
        with self.assertRaises(FulfillmentError) as ctx:
            svc.hold_serial("ed-1", "buyer-b", serial_no=serial)
        self.assertEqual(ctx.exception.kind, "SERIAL_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
