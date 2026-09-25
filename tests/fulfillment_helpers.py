"""履约测试共享装配。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.fulfillment import (
    EventStore,
    FixedClock,
    FulfillmentService,
    StubChainGateway,
)

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
HOLD_TTL = timedelta(minutes=30)


class FulfillmentTestCase(unittest.TestCase):
    db_path: str | None = None

    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.gateway = StubChainGateway()
        if self.db_path:
            self.store = EventStore(self.db_path)
        else:
            self.store = EventStore()
        self.svc = FulfillmentService(self.store, self.gateway, clock=self.clock, hold_ttl=HOLD_TTL)
        self.edition = self.svc.register_edition("草书·临池", 10, reserved_ranges=[(1, 2)])

    def tearDown(self) -> None:
        self.store.close()

    # -- 快捷推进 ---------------------------------------------------------- #
    def pay_order(self, buyer: str = "buyer-1", serial: int = 5, payment_ref: str = "pay-1"):
        hold_id = self.svc.hold_serial(self.edition, buyer, serial=serial)
        order_id = self.svc.confirm_payment(payment_ref, hold_id, 100)
        return hold_id, order_id

    def registered_order(self, buyer: str = "buyer-1", serial: int = 5, payment_ref: str = "pay-1"):
        hold_id, order_id = self.pay_order(buyer, serial, payment_ref)
        self.svc.pass_identity(order_id, {"real_name": "张三", "document_ref": "ID-9"})
        reg_ref = self.svc.submit_registration(order_id)
        return hold_id, order_id, reg_ref

    def slot(self, serial: int = 5):
        return self.store.read_slot(self.edition, serial)


class FileStoreTestCase(FulfillmentTestCase):
    """使用文件库以模拟服务重启。"""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        super().setUp()

    def restart(self) -> None:
        """关闭并重开库，模拟服务重启（时钟与网关为同一实例）。"""
        self.store.close()
        self.store = EventStore(self.db_path)
        self.svc = FulfillmentService(self.store, self.gateway, clock=self.clock, hold_ttl=HOLD_TTL)
