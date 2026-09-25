"""预占临界过期、并发不重号、重复发售防护。"""

from __future__ import annotations

import threading
import unittest
from datetime import timedelta

from src.fulfillment import HoldExpired, SerialUnavailable
from tests.fulfillment_helpers import HOLD_TTL, FulfillmentTestCase


class HoldExpiryTest(FulfillmentTestCase):
    def test_payment_one_second_before_expiry_succeeds(self):
        hold_id = self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        self.clock.set(self.clock.now() + HOLD_TTL - timedelta(seconds=1))
        order_id = self.svc.confirm_payment("pay-edge-1", hold_id, 100)
        self.assertTrue(order_id.startswith("ORD-"))
        self.assertEqual(self.slot(5)["state"], "LINKED")

    def test_payment_exactly_at_expiry_is_rejected_and_released(self):
        hold_id = self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        self.clock.set(self.clock.now() + HOLD_TTL)  # 恰在过期时刻
        with self.assertRaises(HoldExpired):
            self.svc.confirm_payment("pay-edge-2", hold_id, 100)
        self.assertEqual(self.slot(5)["state"], "RELEASED")

    def test_recover_releases_expired_hold_at_exact_boundary(self):
        self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        self.clock.set(self.clock.now() + HOLD_TTL - timedelta(seconds=1))
        result = self.svc.recover()
        self.assertEqual(result["expired_holds"], 0)
        self.assertEqual(self.slot(5)["state"], "HELD")
        self.clock.set(self.clock.now() + timedelta(seconds=1))  # 到达过期时刻
        result = self.svc.recover()
        self.assertEqual(result["expired_holds"], 1)
        self.assertEqual(self.slot(5)["state"], "RELEASED")

    def test_expired_serial_can_be_resold(self):
        self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        self.clock.set(self.clock.now() + HOLD_TTL)
        self.svc.recover()
        hold_id = self.svc.hold_serial(self.edition, "buyer-2", serial=5)
        order_id = self.svc.confirm_payment("pay-resale", hold_id, 100)
        self.assertTrue(order_id.startswith("ORD-"))


class HoldGuardTest(FulfillmentTestCase):
    def test_reserved_range_not_sellable(self):
        with self.assertRaises(SerialUnavailable):
            self.svc.hold_serial(self.edition, "buyer-1", serial=1)

    def test_serial_beyond_total_not_sellable(self):
        with self.assertRaises(SerialUnavailable):
            self.svc.hold_serial(self.edition, "buyer-1", serial=11)

    def test_registered_serial_never_resold(self):
        self.registered_order(serial=5)
        with self.assertRaises(SerialUnavailable):
            self.svc.hold_serial(self.edition, "buyer-2", serial=5)

    def test_hold_command_key_is_idempotent(self):
        h1 = self.svc.hold_serial(self.edition, "buyer-1", serial=5, command_key="cmd-1")
        h2 = self.svc.hold_serial(self.edition, "buyer-1", serial=5, command_key="cmd-1")
        self.assertEqual(h1, h2)


class ConcurrentHoldTest(FulfillmentTestCase):
    def test_concurrent_holds_on_same_serial_single_winner(self):
        results: list[str] = []
        errors: list[Exception] = []

        def race(buyer: str) -> None:
            try:
                results.append(self.svc.hold_serial(self.edition, buyer, serial=5))
            except SerialUnavailable as exc:
                errors.append(exc)

        threads = [threading.Thread(target=race, args=(f"buyer-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, "同一序号只能有一个预占赢家")
        self.assertEqual(len(errors), 7)
        self.assertEqual(self.slot(5)["state"], "HELD")

    def test_concurrent_auto_pick_exhausts_without_duplicates(self):
        # 版次共 10 个序号，1-2 保留，可售 8 个
        results: list[str] = []
        errors: list[Exception] = []

        def race(buyer: str) -> None:
            try:
                results.append(self.svc.hold_serial(self.edition, buyer))
            except SerialUnavailable as exc:
                errors.append(exc)

        threads = [threading.Thread(target=race, args=(f"buyer-{i}",)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 8)
        self.assertEqual(len(errors), 4)
        serials = set()
        for hold_id in results:
            events = self.store.load_events(hold_id)
            serials.add(events[0].payload["serial"])
        self.assertEqual(len(serials), 8, "并发自动分配不得重复发号")


if __name__ == "__main__":
    unittest.main()
