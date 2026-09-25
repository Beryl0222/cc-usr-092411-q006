"""支付/登记/退件回执的完全重放与内容变化隔离。"""

from __future__ import annotations

import unittest

from src.fulfillment import ChainReceipt, ReceiptMismatch
from src.fulfillment import events as ev
from tests.fulfillment_helpers import FulfillmentTestCase


def count_events(store, aggregate_id: str, event_type: str) -> int:
    return sum(1 for e in store.load_events(aggregate_id) if e.event_type == event_type)


def open_cases(store) -> list:
    return [e for e in store.all_events() if e.event_type == ev.EVENT_REMEDY_CASE_OPENED]


class PaymentReceiptTest(FulfillmentTestCase):
    def test_exact_replay_returns_same_order_without_new_events(self):
        hold_id = self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        order_id = self.svc.confirm_payment("pay-1", hold_id, 100)
        replayed = self.svc.confirm_payment("pay-1", hold_id, 100)
        self.assertEqual(order_id, replayed)
        self.assertEqual(count_events(self.store, order_id, ev.EVENT_PAYMENT_CONFIRMED), 1)

    def test_content_change_is_rejected_and_isolated(self):
        hold_id, order_id = self.pay_order()
        with self.assertRaises(ReceiptMismatch):
            self.svc.confirm_payment("pay-1", hold_id, 200)  # 同一回执，金额变了
        cases = open_cases(self.store)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].payload["category"], "CONTENT_ISOLATION")
        # 订单被隔离，后续履约暂停
        order = self.svc._load_order(order_id)
        self.assertTrue(order.quarantined)


class RegistrationReceiptTest(FulfillmentTestCase):
    def test_exact_replay_returns_same_outcome(self):
        _, order_id, reg_ref = self.registered_order()
        # 首次回执由 submit_registration 经 stub 网关消费（chain_tx_hash=0xstub-<ref>）；
        # 完全相同的回执再次投递应返回原结果且不产生新事件
        receipt = ChainReceipt(registration_ref=reg_ref, accepted=True, chain_tx_hash=f"0xstub-{reg_ref}")
        first = self.svc.apply_registration_receipt(receipt)
        second = self.svc.apply_registration_receipt(receipt)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(count_events(self.store, order_id, ev.EVENT_REGISTRATION_ACCEPTED), 1)

    def test_content_change_is_rejected_and_isolated(self):
        hold_id = self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        order_id = self.svc.confirm_payment("pay-1", hold_id, 100)
        self.svc.pass_identity(order_id, {"real_name": "张三", "document_ref": "ID-9"})
        reg_ref = self.svc.submit_registration(order_id)  # 默认成功回执
        with self.assertRaises(ReceiptMismatch):
            self.svc.apply_registration_receipt(
                ChainReceipt(registration_ref=reg_ref, accepted=False, reason="链上回执内容矛盾")
            )
        cases = open_cases(self.store)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].payload["category"], "CONTENT_ISOLATION")

    def test_terminal_failure_releases_serial_for_resale(self):
        hold_id = self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        order_id = self.svc.confirm_payment("pay-1", hold_id, 100)
        self.svc.pass_identity(order_id, {"real_name": "张三", "document_ref": "ID-9"})
        self.gateway.script_error("REG-FAIL", ConnectionError("网关不可用"))  # 提交留待 outbox
        reg_ref = self.svc.submit_registration(order_id, registration_ref="REG-FAIL")
        outcome = self.svc.apply_registration_receipt(
            ChainReceipt(registration_ref=reg_ref, accepted=False, reason="链上拒绝", retriable=False)
        )
        self.assertEqual(outcome["status"], "failed_released")
        self.assertEqual(self.slot(5)["state"], "RELEASED")
        # 释放后序号可再次发售
        hold2 = self.svc.hold_serial(self.edition, "buyer-2", serial=5)
        self.assertTrue(hold2.startswith("HOLD-"))

    def test_retriable_failure_keeps_order_waiting(self):
        hold_id = self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        order_id = self.svc.confirm_payment("pay-1", hold_id, 100)
        self.svc.pass_identity(order_id, {"real_name": "张三", "document_ref": "ID-9"})
        self.gateway.script_error("REG-RETRY", ConnectionError("网关不可用"))
        reg_ref = self.svc.submit_registration(order_id, registration_ref="REG-RETRY")
        outcome = self.svc.apply_registration_receipt(
            ChainReceipt(registration_ref=reg_ref, accepted=False, reason="节点超时", retriable=True)
        )
        self.assertEqual(outcome["status"], "retry_pending")
        self.assertEqual(self.slot(5)["state"], "LINKED")  # 未释放
        self.assertEqual(len(self.store.pending_registrations()), 1)


class ReturnReceiptTest(FulfillmentTestCase):
    def test_return_receipt_replay_processed_once(self):
        _, order_id, _ = self.registered_order()
        self.svc.create_shipment(order_id, "张三", "北京市东城区 1 号", "13800000000")
        self.svc.dispatch(order_id, "顺丰", "SF123")
        shipment_no = f"SHIP-{order_id}"

        self.assertTrue(self.svc.receive_return("ret-1", shipment_no, "收件人拒收"))
        self.assertFalse(self.svc.receive_return("ret-1", shipment_no, "收件人拒收"))

        cases = open_cases(self.store)
        self.assertEqual(len(cases), 1, "同一退件回执只立案一次")
        self.assertEqual(cases[0].payload["category"], "SHIPMENT_RETURN")
        # 数字归属不回滚
        self.assertEqual(self.slot(5)["state"], "REGISTERED")


if __name__ == "__main__":
    unittest.main()
