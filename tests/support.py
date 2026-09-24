"""履约链测试共享基座：可注入时钟 + 临时事件日志 + 重启模拟。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.clock import ManualClock
from src.service import FulfillmentService

HOLD_TTL = timedelta(minutes=30)


class FulfillmentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self._tmp.name) / "events.jsonl"
        self.clock = ManualClock(datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc))
        self.service = FulfillmentService(self.store_path, clock=self.clock, hold_ttl=HOLD_TTL)

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    def restart(self) -> None:
        """关闭并以同一事件日志重建服务，模拟进程重启。"""
        self.service.close()
        self.service = FulfillmentService(self.store_path, clock=self.clock, hold_ttl=HOLD_TTL)

    def paid_order(self, buyer_id: str, payment_id: str, amount: int = 12800, **kwargs):
        """预占 + 付款，返回 (hold, pay)。"""
        hold = self.service.hold_serial("ed-1", buyer_id)
        pay = self.service.confirm_payment(hold["reservation_id"], payment_id, amount, **kwargs)
        return hold, pay

    def owned_order(self, buyer_id: str, payment_id: str, **kwargs):
        """走完付款、实名复核、链上登记，返回 (hold, pay)。"""
        hold, pay = self.paid_order(buyer_id, payment_id, **kwargs)
        self.service.approve_realname(pay["order_id"], reviewer="ops-1")
        self.service.submit_registration(pay["order_id"])
        self.service.confirm_registration(pay["order_id"], f"rcpt-{payment_id}", f"0x-{payment_id}")
        return hold, pay
