"""部分履约、三类释放、地址审批、内容隔离、重启恢复与序号溯源。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from src.fulfillment import (
    AddressChangePending,
    ChainReceipt,
    IllegalTransition,
    RegistrationIrreversible,
    ShipmentNotOutbound,
)
from src.fulfillment import events as ev
from tests.fulfillment_helpers import HOLD_TTL, FileStoreTestCase, FulfillmentTestCase


class PartialFulfillmentTest(FulfillmentTestCase):
    def test_shipment_waits_for_ownership_confirmation(self):
        _, order_id = self.pay_order()
        # 仅付款（购买资格）不能生成发运任务
        with self.assertRaises(IllegalTransition):
            self.svc.create_shipment(order_id, "张三", "北京市东城区 1 号", "13800000000")
        self.svc.pass_identity(order_id, {"real_name": "张三", "document_ref": "ID-9"})
        # 实名通过但链上未确认，仍不能发运
        with self.assertRaises(IllegalTransition):
            self.svc.create_shipment(order_id, "张三", "北京市东城区 1 号", "13800000000")
        self.svc.submit_registration(order_id)
        # 归属确认后按同一归属生成发运任务
        shipment_no = self.svc.create_shipment(order_id, "张三", "北京市东城区 1 号", "13800000000")
        self.assertEqual(shipment_no, f"SHIP-{order_id}")

    def test_full_lifecycle_partial_then_complete(self):
        _, order_id, _ = self.registered_order()
        order = self.svc._load_order(order_id)
        self.assertEqual(order.status, "REGISTERED")  # 数字履约完成、实体未发的部分履约态
        self.svc.create_shipment(order_id, "张三", "北京市东城区 1 号", "13800000000")
        self.svc.dispatch(order_id, "顺丰", "SF123")
        self.assertEqual(self.svc._load_order(order_id).status, "DISPATCHED")
        self.svc.deliver(order_id)
        self.assertEqual(self.svc._load_order(order_id).status, "DELIVERED")


class ReleasePathTest(FulfillmentTestCase):
    def test_payment_revoke_releases_serial_before_registration(self):
        self.pay_order()
        self.svc.revoke_payment("pay-1", "买家取消")
        self.assertEqual(self.slot(5)["state"], "RELEASED")
        # 释放后可再次发售，杜绝“退款后重复发售”的占用残留
        hold2 = self.svc.hold_serial(self.edition, "buyer-2", serial=5)
        self.assertTrue(hold2.startswith("HOLD-"))

    def test_payment_revoke_after_registration_is_irreversible(self):
        self.registered_order()
        with self.assertRaises(RegistrationIrreversible):
            self.svc.revoke_payment("pay-1", "买家反悔")
        self.assertEqual(self.slot(5)["state"], "REGISTERED")

    def test_identity_rejection_releases_serial(self):
        _, order_id = self.pay_order()
        self.svc.reject_identity(order_id, "证件模糊")
        self.assertEqual(self.slot(5)["state"], "RELEASED")

    def test_late_registration_after_revoke_enters_remedy(self):
        hold_id = self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        order_id = self.svc.confirm_payment("pay-1", hold_id, 100)
        self.svc.pass_identity(order_id, {"real_name": "张三", "document_ref": "ID-9"})
        self.gateway.script_error("REG-LATE", ConnectionError("网关不可用"))
        reg_ref = self.svc.submit_registration(order_id, registration_ref="REG-LATE")
        # 登记在途时支付撤销：释放尚未完成的资源
        self.svc.revoke_payment("pay-1", "超时未确认退款")
        self.assertEqual(self.slot(5)["state"], "RELEASED")
        # 链上迟到成功：数字归属不可回滚，恢复绑定并立案补救
        self.gateway.clear_error("REG-LATE")
        outcome = self.svc.apply_registration_receipt(
            ChainReceipt(registration_ref=reg_ref, accepted=True, chain_tx_hash="0xlate")
        )
        self.assertEqual(outcome["status"], "late_case")
        self.assertEqual(self.slot(5)["state"], "REGISTERED")
        order = self.svc._load_order(order_id)
        self.assertEqual(order.status, "REMEDY")
        cases = [e for e in self.store.all_events() if e.event_type == ev.EVENT_REMEDY_CASE_OPENED]
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].payload["category"], "LATE_REGISTRATION")


class AddressChangeTest(FulfillmentTestCase):
    def setUp(self):
        super().setUp()
        _, self.order_id, _ = self.registered_order()
        self.svc.create_shipment(self.order_id, "张三", "北京市东城区 1 号", "13800000000")

    def test_change_requires_approval_before_dispatch(self):
        self.svc.request_address_change(self.order_id, "上海市徐汇区 2 号", "客服A")
        # 待审批变更未决，不能出库
        with self.assertRaises(IllegalTransition):
            self.svc.dispatch(self.order_id, "顺丰", "SF123")
        self.svc.approve_address_change(self.order_id, "运营B")
        self.svc.dispatch(self.order_id, "顺丰", "SF123")
        order = self.svc._load_order(self.order_id)
        self.assertEqual(order.shipment.address, "上海市徐汇区 2 号")

    def test_rejected_change_keeps_original_address(self):
        self.svc.request_address_change(self.order_id, "上海市徐汇区 2 号", "客服A")
        self.svc.reject_address_change(self.order_id, "运营B")
        self.svc.dispatch(self.order_id, "顺丰", "SF123")
        order = self.svc._load_order(self.order_id)
        self.assertEqual(order.shipment.address, "北京市东城区 1 号")

    def test_second_change_while_pending_rejected(self):
        self.svc.request_address_change(self.order_id, "上海市徐汇区 2 号", "客服A")
        with self.assertRaises(AddressChangePending):
            self.svc.request_address_change(self.order_id, "广州市天河区 3 号", "客服A")

    def test_no_change_after_dispatch(self):
        self.svc.dispatch(self.order_id, "顺丰", "SF123")
        with self.assertRaises(ShipmentNotOutbound):
            self.svc.request_address_change(self.order_id, "上海市徐汇区 2 号", "客服A")


class ContentIsolationTest(FulfillmentTestCase):
    def test_quarantine_blocks_dispatch_until_resolved(self):
        _, order_id, _ = self.registered_order()
        self.svc.create_shipment(order_id, "张三", "北京市东城区 1 号", "13800000000")
        case_id = self.svc.open_content_isolation(
            self.edition, 5, "作品文件哈希与登记摘要不一致", order_id=order_id
        )
        with self.assertRaises(IllegalTransition):
            self.svc.dispatch(order_id, "顺丰", "SF123")
        self.svc.resolve_case(case_id, "复核为误报，恢复履约")
        self.svc.dispatch(order_id, "顺丰", "SF123")
        self.assertEqual(self.svc._load_order(order_id).status, "DISPATCHED")


class RestartRecoveryTest(FileStoreTestCase):
    def test_restart_continues_expiry_registration_and_returns(self):
        # 1) 一条将过期的预占
        self.svc.hold_serial(self.edition, "buyer-exp", serial=6)
        # 2) 一笔登记因网关故障滞留 outbox
        hold_id = self.svc.hold_serial(self.edition, "buyer-1", serial=5)
        order_id = self.svc.confirm_payment("pay-1", hold_id, 100)
        self.svc.pass_identity(order_id, {"real_name": "张三", "document_ref": "ID-9"})
        self.gateway.script_error("REG-DOWN", ConnectionError("网关不可用"))
        self.svc.submit_registration(order_id, registration_ref="REG-DOWN")
        self.assertEqual(len(self.store.pending_registrations()), 1)
        # 3) 一笔退件滞留 inbox（模拟接收后未处理即宕机）
        _, order2, _ = self.registered_order(buyer="buyer-2", serial=7, payment_ref="pay-2")
        self.svc.create_shipment(order2, "李四", "杭州市西湖区 4 号", "13900000000")
        self.svc.dispatch(order2, "顺丰", "SF777")
        with self.store.transaction() as conn:
            self.store.receive_return(conn, "ret-9", f"SHIP-{order2}", "地址无人签收",
                                      self.clock.now().isoformat())

        # 重启：时钟越过预占过期线，网关恢复
        self.clock.set(self.clock.now() + HOLD_TTL + timedelta(seconds=1))
        self.gateway.clear_error("REG-DOWN")
        self.restart()

        result = self.svc.recover()
        self.assertEqual(result["expired_holds"], 1)
        self.assertEqual(result["registrations_driven"], 1)
        self.assertEqual(result["returns_processed"], 1)

        # 过期预占已释放
        self.assertEqual(self.slot(6)["state"], "RELEASED")
        # 滞留登记已补上，归属确认
        self.assertEqual(self.slot(5)["state"], "REGISTERED")
        self.assertEqual(self.svc._load_order(order_id).status, "REGISTERED")
        # 退件已立案，数字归属不回滚
        self.assertEqual(self.slot(7)["state"], "REGISTERED")
        self.assertEqual(self.svc._load_order(order2).status, "REMEDY")
        self.assertTrue(self.store.return_processed("ret-9"))
        # 再次恢复为空转（幂等）
        again = self.svc.recover()
        self.assertEqual(again, {"expired_holds": 0, "registrations_driven": 0, "returns_processed": 0})

    def test_slot_projection_rebuilds_from_events(self):
        self.registered_order(serial=5)
        self.restart()
        self.store.rebuild_slots()
        self.assertEqual(self.slot(5)["state"], "REGISTERED")


class TraceSerialTest(FulfillmentTestCase):
    def test_trace_reconstructs_full_chain(self):
        hold_id, order_id, reg_ref = self.registered_order()
        self.svc.create_shipment(order_id, "张三", "北京市东城区 1 号", "13800000000")
        self.svc.request_address_change(order_id, "上海市徐汇区 2 号", "客服A")
        self.svc.approve_address_change(order_id, "运营B")
        self.svc.dispatch(order_id, "顺丰", "SF123")
        self.svc.receive_return("ret-1", f"SHIP-{order_id}", "收件人拒收")

        trace = self.svc.trace_serial(self.edition, 5)
        self.assertEqual(trace["slot"]["state"], "REGISTERED")
        self.assertTrue(any(e["event_type"] == "SERIAL_HELD" for e in trace["reservation"]))
        self.assertTrue(any(e["event_type"] == "SERIAL_OWNERSHIP_REGISTERED" for e in trace["reservation"]))
        self.assertTrue(any(e["event_type"] == "PAYMENT_CONFIRMED" for e in trace["payment"]))
        self.assertTrue(any(e["event_type"] == "REGISTRATION_ACCEPTED" for e in trace["registration"]))
        ship_types = {e["event_type"] for e in trace["shipment"]}
        self.assertIn("SHIPMENT_TASK_CREATED", ship_types)
        self.assertIn("ADDRESS_CHANGE_APPROVED", ship_types)
        self.assertIn("PHYSICAL_DISPATCHED", ship_types)
        self.assertIn("SHIPMENT_RETURNED", ship_types)
        remedy_types = {e["event_type"] for e in trace["remedy"]}
        self.assertIn("REMEDY_CASE_OPENED", remedy_types)
        self.assertIn("ORDER_REMEDIED", remedy_types)


if __name__ == "__main__":
    unittest.main()
