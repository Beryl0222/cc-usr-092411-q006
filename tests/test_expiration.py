"""临界过期：预占 expires_at 前后一秒与恰好到期时刻的行为。"""

import unittest
from datetime import timedelta

from src.service import FulfillmentError

from support import HOLD_TTL, FulfillmentTestCase


class ExpirationTest(FulfillmentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.register_edition("ed-1", total_supply=5)

    def test_not_expired_one_second_before_deadline(self) -> None:
        svc = self.service
        hold = svc.hold_serial("ed-1", "buyer-a")
        self.clock.advance(HOLD_TTL - timedelta(seconds=1))
        self.assertEqual(svc.sweep_expired_holds(), [])
        self.assertEqual(svc.serial_status("ed-1", hold["serial_no"]), "HELD")
        pay = svc.confirm_payment(hold["reservation_id"], "pay-1", 12800)
        self.assertEqual(pay["status"], "CONFIRMED")

    def test_expired_exactly_at_deadline(self) -> None:
        svc = self.service
        hold = svc.hold_serial("ed-1", "buyer-a")
        self.clock.advance(HOLD_TTL)  # 恰好到达过期时刻
        with self.assertRaises(FulfillmentError) as ctx:
            svc.confirm_payment(hold["reservation_id"], "pay-1", 12800)
        self.assertEqual(ctx.exception.kind, "HOLD_EXPIRED")
        self.assertEqual(svc.sweep_expired_holds(), [hold["reservation_id"]])
        self.assertEqual(svc.serial_status("ed-1", hold["serial_no"]), "AVAILABLE")

    def test_expired_hold_does_not_block_serial_before_sweep(self) -> None:
        svc = self.service
        hold = svc.hold_serial("ed-1", "buyer-a")
        serial = hold["serial_no"]
        self.clock.advance(HOLD_TTL + timedelta(seconds=1))
        # 尚未清扫，但过期预占不再占用序号
        self.assertEqual(svc.serial_status("ed-1", serial), "AVAILABLE")
        hold2 = svc.hold_serial("ed-1", "buyer-b", serial_no=serial)
        pay = svc.confirm_payment(hold2["reservation_id"], "pay-2", 12800)
        self.assertEqual(pay["status"], "CONFIRMED")
        self.assertEqual(svc.serial_status("ed-1", serial), "SOLD")

    def test_sweep_only_releases_expired(self) -> None:
        svc = self.service
        old = svc.hold_serial("ed-1", "buyer-a")
        self.clock.advance(HOLD_TTL - timedelta(seconds=1))
        fresh = svc.hold_serial("ed-1", "buyer-b")
        self.clock.advance(timedelta(seconds=1))  # old 到期，fresh 还有 30 分钟
        self.assertEqual(svc.sweep_expired_holds(), [old["reservation_id"]])
        self.assertEqual(svc.serial_status("ed-1", fresh["serial_no"]), "HELD")


if __name__ == "__main__":
    unittest.main()
