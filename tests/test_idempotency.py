"""重复回执：完全重放返回原结果，内容变化隔离调查。"""

import unittest

from support import FulfillmentTestCase


class IdempotencyTest(FulfillmentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.register_edition("ed-1", total_supply=5, physical_quota=1)

    def test_payment_exact_replay_returns_original(self) -> None:
        svc = self.service
        hold, pay = self.paid_order("buyer-a", "pay-1", wants_physical=True, address="地址甲")
        before = len(svc.events)
        again = svc.confirm_payment(hold["reservation_id"], "pay-1", 12800,
                                    wants_physical=True, address="地址甲")
        self.assertEqual(again["status"], "REPLAYED")
        self.assertEqual(again["order_id"], pay["order_id"])
        self.assertEqual(len(svc.events), before)  # 不产生新事件
        trace = svc.trace_serial("ed-1", hold["serial_no"])
        self.assertEqual(len(trace["orders"]), 1)

    def test_payment_content_change_quarantined(self) -> None:
        svc = self.service
        hold, pay = self.paid_order("buyer-a", "pay-1", wants_physical=True, address="地址甲")
        before = len(svc.events)
        changed = svc.confirm_payment(hold["reservation_id"], "pay-1", 99999,
                                      wants_physical=True, address="地址甲")  # 金额被篡改
        self.assertEqual(changed["status"], "QUARANTINED")
        self.assertEqual(changed["order_id"], pay["order_id"])
        self.assertEqual(len(svc.events), before + 1)  # 仅一条隔离事件
        trace = svc.trace_serial("ed-1", hold["serial_no"])
        self.assertEqual(len(trace["orders"]), 1)
        self.assertEqual(trace["orders"][0]["amount"], 12800)  # 原结果不变
        self.assertEqual(len(trace["quarantined"]), 1)
        self.assertEqual(trace["quarantined"][0]["kind"], "payment")
        self.assertEqual(trace["quarantined"][0]["received"]["amount"], 99999)
        self.assertEqual(svc.serial_status("ed-1", hold["serial_no"]), "SOLD")

    def test_registration_exact_replay_returns_original(self) -> None:
        svc = self.service
        hold, pay = self.paid_order("buyer-a", "pay-1")
        svc.approve_realname(pay["order_id"], reviewer="ops-1")
        svc.submit_registration(pay["order_id"])
        svc.confirm_registration(pay["order_id"], "rcpt-1", "0xabc")
        before = len(svc.events)
        again = svc.confirm_registration(pay["order_id"], "rcpt-1", "0xabc")
        self.assertEqual(again["status"], "REPLAYED")
        self.assertEqual(again["order_id"], pay["order_id"])
        self.assertEqual(len(svc.events), before)
        types = [e["event_type"] for e in svc.events]
        self.assertEqual(types.count("OWNERSHIP_CONFIRMED"), 1)  # 归属只确认一次

    def test_registration_content_change_quarantined(self) -> None:
        svc = self.service
        hold, pay = self.paid_order("buyer-a", "pay-1")
        svc.approve_realname(pay["order_id"], reviewer="ops-1")
        svc.submit_registration(pay["order_id"])
        svc.confirm_registration(pay["order_id"], "rcpt-1", "0xabc")
        changed = svc.confirm_registration(pay["order_id"], "rcpt-1", "0x伪造哈希")
        self.assertEqual(changed["status"], "QUARANTINED")
        order = svc.order_state(pay["order_id"])
        self.assertEqual(order["tx_hash"], "0xabc")  # 原登记结果不变
        self.assertEqual(order["status"], "OWNERSHIP_CONFIRMED")
        self.assertEqual(len(svc.quarantined_receipts), 1)
        self.assertEqual(svc.quarantined_receipts[0]["kind"], "registration")

    def test_replay_survives_restart(self) -> None:
        svc = self.service
        hold, pay = self.paid_order("buyer-a", "pay-1")
        self.restart()
        svc = self.service
        again = svc.confirm_payment(hold["reservation_id"], "pay-1", 12800)
        self.assertEqual(again["status"], "REPLAYED")
        self.assertEqual(again["order_id"], pay["order_id"])
        svc.approve_realname(pay["order_id"], reviewer="ops-1")
        svc.submit_registration(pay["order_id"])
        svc.confirm_registration(pay["order_id"], "rcpt-1", "0xabc")
        self.restart()
        replay = self.service.confirm_registration(pay["order_id"], "rcpt-1", "0xabc")
        self.assertEqual(replay["status"], "REPLAYED")


if __name__ == "__main__":
    unittest.main()
