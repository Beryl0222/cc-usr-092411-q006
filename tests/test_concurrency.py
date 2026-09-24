"""并发购买：同一序号不会被两个买家获得。"""

import threading
import unittest

from src.service import FulfillmentError

from support import FulfillmentTestCase


class ConcurrencyTest(FulfillmentTestCase):
    def _hammer(self, threads_n: int, worker) -> tuple[list, list]:
        barrier = threading.Barrier(threads_n)
        results: list = []
        errors: list = []
        lock = threading.Lock()

        def run(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                outcome = worker(i)
                with lock:
                    results.append(outcome)
            except FulfillmentError as exc:
                with lock:
                    errors.append(exc.kind)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return results, errors

    def test_concurrent_holds_get_distinct_serials(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5)
        results, errors = self._hammer(12, lambda i: svc.hold_serial("ed-1", f"buyer-{i}"))
        self.assertEqual(len(results), 5)
        self.assertEqual(len({r["serial_no"] for r in results}), 5)  # 序号不重复
        self.assertEqual(sorted(errors), ["NO_SERIAL_AVAILABLE"] * 7)

    def test_concurrent_same_serial_single_winner(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5)
        results, errors = self._hammer(8, lambda i: svc.hold_serial("ed-1", f"buyer-{i}", serial_no=2))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["serial_no"], 2)
        self.assertEqual(sorted(errors), ["SERIAL_UNAVAILABLE"] * 7)

    def test_concurrent_payments_on_same_reservation_single_order(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=5)
        hold = svc.hold_serial("ed-1", "buyer-a")
        results, errors = self._hammer(
            6, lambda i: svc.confirm_payment(hold["reservation_id"], f"pay-{i}", 12800))
        confirmed = [r for r in results if r["status"] == "CONFIRMED"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(sorted(errors), ["INVALID_STATE"] * 5)
        trace = svc.trace_serial("ed-1", hold["serial_no"])
        self.assertEqual(len(trace["orders"]), 1)


if __name__ == "__main__":
    unittest.main()
