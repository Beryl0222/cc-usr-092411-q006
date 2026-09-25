"""应用服务：统一履约链的用例编排。

职责边界：

- 领域规则在 ``model`` 聚合内；本层负责加载/保存聚合、幂等、事务与外部网关。
- 时钟可注入：预占过期、恢复扫描都以注入时钟为准。
- 支付回执与登记回执幂等：完全重放返回原结果；内容变化触发内容隔离调查并拒绝。
- 重启恢复：``recover()`` 继续过期释放、补登记（outbox）与退件处理（inbox）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from . import events as ev
from .clock import Clock, SystemClock
from .errors import (
    FulfillmentError,
    HoldExpired,
    ReceiptMismatch,
    SerialUnavailable,
)
from .gateway import ChainGateway, ChainReceipt
from .model import (
    CASE_CONTENT_ISOLATION,
    CASE_LATE_REGISTRATION,
    CASE_SHIPMENT_RETURN,
    Edition,
    PurchaseOrder,
    RemedyCase,
    SerialReservation,
    ORDER_RELEASED,
    REL_CHAIN_FAILED,
    REL_PAYMENT_REVOKED,
)
from .store import EventStore, ReceiptConflict, SerialSlotTaken

DEFAULT_HOLD_TTL = timedelta(minutes=30)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class FulfillmentService:
    def __init__(
        self,
        store: EventStore,
        gateway: ChainGateway,
        clock: Clock | None = None,
        hold_ttl: timedelta = DEFAULT_HOLD_TTL,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.clock: Clock = clock or SystemClock()
        self.hold_ttl = hold_ttl
        self._id = id_factory

    # ------------------------------------------------------------------ #
    # 加载辅助
    # ------------------------------------------------------------------ #
    def _load_edition(self, edition_id: str) -> Edition:
        edition = Edition.load(edition_id, self.store.load_events(edition_id))
        if edition.title is None:
            raise FulfillmentError(f"版次 {edition_id} 尚未登记")
        return edition

    def _load_hold(self, hold_id: str) -> SerialReservation:
        return SerialReservation.load(hold_id, self.store.load_events(hold_id))

    def _load_order(self, order_id: str) -> PurchaseOrder:
        return PurchaseOrder.load(order_id, self.store.load_events(order_id))

    def _load_case(self, case_id: str) -> RemedyCase:
        return RemedyCase.load(case_id, self.store.load_events(case_id))

    def _save(self, conn, *aggregates) -> None:
        events: list[ev.Event] = []
        for agg in aggregates:
            events.extend(agg.pull_events())
        self.store.append_events(conn, events)

    # ------------------------------------------------------------------ #
    # 版次登记
    # ------------------------------------------------------------------ #
    def register_edition(
        self,
        title: str,
        total_serials: int,
        reserved_ranges: list[tuple[int, int]] | None = None,
        edition_id: str | None = None,
    ) -> str:
        edition_id = edition_id or self._id("ED")
        if self.store.load_events(edition_id):
            raise FulfillmentError(f"版次 {edition_id} 已登记，总量与保留范围不可改写")
        edition = Edition.register(edition_id, title, total_serials, reserved_ranges, self.clock.now())
        with self.store.transaction() as conn:
            self._save(conn, edition)
        return edition_id

    # ------------------------------------------------------------------ #
    # 序号预占（可注入时钟过期）
    # ------------------------------------------------------------------ #
    def hold_serial(
        self,
        edition_id: str,
        buyer_ref: str,
        serial: int | None = None,
        command_key: str | None = None,
    ) -> str:
        """预占一个序号，返回 hold_id。并发下同一序号只会有一个赢家。"""
        now = self.clock.now()
        if command_key:
            existing = self.store.command_result(f"hold:{command_key}")
            if existing:
                return existing
        edition = self._load_edition(edition_id)
        with self.store.transaction() as conn:
            if serial is None:
                serial = self.store.first_free_serial(conn, edition_id, edition.sellable_serials())
                if serial is None:
                    raise SerialUnavailable("版次已无可售序号")
            else:
                edition.assert_sellable(serial)
                slot = self.store.slot_state(conn, edition_id, serial)
                if slot is not None and slot["state"] != "RELEASED":
                    raise SerialUnavailable(f"序号 {serial} 当前不可售（{slot['state']}）")
            hold_id = self._id("HOLD")
            hold = SerialReservation.create(
                hold_id, edition_id, serial, buyer_ref, now + self.hold_ttl, now
            )
            try:
                self._save(conn, hold)
            except SerialSlotTaken:
                raise SerialUnavailable(f"序号 {serial} 刚被其他买家预占") from None
            if command_key:
                self.store.remember_command(conn, f"hold:{command_key}", hold_id, now.isoformat())
        return hold_id

    # ------------------------------------------------------------------ #
    # 支付（只锁定购买资格；回执幂等）
    # ------------------------------------------------------------------ #
    def confirm_payment(self, payment_ref: str, hold_id: str, amount: int) -> str:
        """按支付回执确认付款，返回 order_id。同一回执重放返回原订单。"""
        now = self.clock.now()
        request_key = {"hold_id": hold_id, "amount": amount}

        # 恰在付款时过期的预占：先在独立事务内持久化释放，再拒绝受理
        expired_serial: int | None = None
        with self.store.transaction() as conn:
            hold = self._load_hold(hold_id)
            if hold.state == "HELD" and now >= hold.expires_at:
                hold.expire_if_due(now)
                self._save(conn, hold)
                expired_serial = hold.serial
        if expired_serial is not None:
            raise HoldExpired(f"序号 {expired_serial} 预占已过期，支付回执 {payment_ref} 不予受理")

        order_id = self._id("ORD")
        conflict = False
        with self.store.transaction() as conn:
            try:
                replay = self.store.check_receipt(conn, "payment", payment_ref, request_key)
            except ReceiptConflict:
                conflict = True
            else:
                if replay is not None:
                    return replay["order_id"]
                hold = self._load_hold(hold_id)
                hold.assert_active(now)  # 失败则整体回滚，回执不留痕
                order = PurchaseOrder.create(
                    order_id,
                    hold_id=hold_id,
                    edition_id=hold.edition_id,
                    serial=hold.serial,
                    buyer_ref=hold.buyer_ref,
                    payment_ref=payment_ref,
                    amount=amount,
                    now=now,
                )
                hold.link_order(order_id, payment_ref, now)
                self._save(conn, order, hold)
                self.store.remember_receipt(
                    conn, "payment", payment_ref, request_key, {"order_id": order_id}, now.isoformat()
                )
        if conflict:
            self._isolate_for_receipt_conflict(hold_id=hold_id, receipt=f"payment:{payment_ref}")
            raise ReceiptMismatch(f"支付回执 {payment_ref} 与首次请求内容不一致，已隔离调查")
        return order_id

    def revoke_payment(self, payment_ref: str, reason: str) -> None:
        """支付撤销：释放尚未完成归属确认的序号；已登记的归属不可回滚。"""
        now = self.clock.now()
        order_id = self._find_order_by_payment(payment_ref)
        if order_id is None:
            raise FulfillmentError(f"找不到支付回执 {payment_ref} 对应的订单")
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            if order.status == ORDER_RELEASED:
                return  # 幂等：已释放
            order.revoke_payment(now, reason)
            hold = self._load_hold(order.hold_id)
            hold.release(REL_PAYMENT_REVOKED, now)
            self._save(conn, order, hold)

    def _find_order_by_payment(self, payment_ref: str) -> str | None:
        for event in self.store.load_events_by_type(
            ev.AGG_PURCHASE_ORDER, (ev.EVENT_PAYMENT_CONFIRMED,)
        ):
            if event.payload.get("payment_ref") == payment_ref:
                return event.aggregate_id
        return None

    # ------------------------------------------------------------------ #
    # 实名复核
    # ------------------------------------------------------------------ #
    def pass_identity(self, order_id: str, materials: dict[str, Any]) -> None:
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            order.pass_identity(materials, self.clock.now())
            self._save(conn, order)

    def reject_identity(self, order_id: str, reason: str) -> None:
        now = self.clock.now()
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            if order.status == ORDER_RELEASED:
                return
            order.reject_identity(reason, now)
            hold = self._load_hold(order.hold_id)
            hold.release("identity_rejected", now)
            self._save(conn, order, hold)

    # ------------------------------------------------------------------ #
    # 链上登记（outbox + 回执幂等 + 补登记）
    # ------------------------------------------------------------------ #
    def submit_registration(self, order_id: str, registration_ref: str | None = None) -> str:
        """实名通过后提交链上登记；立即尝试一次，失败留待恢复扫描补登记。"""
        now = self.clock.now()
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            if order.registration_ref:
                return order.registration_ref  # 幂等：已提交过
            registration_ref = registration_ref or self._id("REG")
            order.submit_registration(registration_ref, now)
            self.store.enqueue_registration(conn, registration_ref, order_id, now.isoformat())
            self._save(conn, order)
        self._drive_registration(registration_ref)
        return registration_ref

    def _drive_registration(self, registration_ref: str) -> None:
        """向网关提交一次登记并应用回执；网关异常时保持 PENDING 等待恢复。"""
        now = self.clock.now()
        pending = {r["registration_ref"]: r for r in self.store.pending_registrations()}
        row = pending.get(registration_ref)
        if row is None:
            return
        order = self._load_order(row["order_id"])
        try:
            receipt = self.gateway.submit(registration_ref, order.id, order.edition_id, order.serial)
        except Exception as exc:  # 网关不可用：记录尝试，等待重启/扫描补登记
            with self.store.transaction() as conn:
                self.store.touch_registration_attempt(conn, registration_ref, str(exc), now.isoformat())
            return
        self.apply_registration_receipt(receipt)

    def apply_registration_receipt(self, receipt: ChainReceipt) -> dict[str, Any]:
        """应用登记回执。完全重放返回原结果；内容变化隔离调查并拒绝。"""
        now = self.clock.now()
        request_key = {
            "accepted": receipt.accepted,
            "chain_tx_hash": receipt.chain_tx_hash,
            "reason": receipt.reason,
            "retriable": receipt.retriable,
        }
        conflict = False
        outcome: dict[str, Any] | None = None
        with self.store.transaction() as conn:
            try:
                replay = self.store.check_receipt(conn, "registration", receipt.registration_ref, request_key)
            except ReceiptConflict:
                conflict = True
            else:
                if replay is not None:
                    outcome = replay
                else:
                    outcome = self._apply_receipt_first_time(conn, receipt, now)
                    self.store.remember_receipt(
                        conn, "registration", receipt.registration_ref, request_key, outcome, now.isoformat()
                    )
        if conflict:
            self._isolate_for_receipt_conflict(registration_ref=receipt.registration_ref,
                                               receipt=f"registration:{receipt.registration_ref}")
            raise ReceiptMismatch(f"登记回执 {receipt.registration_ref} 内容前后不一致，已隔离调查")
        return outcome

    def _apply_receipt_first_time(self, conn, receipt: ChainReceipt, now: datetime) -> dict[str, Any]:
        pending = {r["registration_ref"]: r for r in self.store.pending_registrations()}
        row = pending.get(receipt.registration_ref)
        order_id = row["order_id"] if row else self._find_order_by_registration(receipt.registration_ref)
        if order_id is None:
            raise FulfillmentError(f"找不到登记回执 {receipt.registration_ref} 对应的订单")
        order = self._load_order(order_id)
        hold = self._load_hold(order.hold_id)

        if receipt.accepted:
            normal = order.registration_accepted(receipt.registration_ref, receipt.chain_tx_hash, now)
            if normal:
                hold.mark_registered(receipt.registration_ref, receipt.chain_tx_hash, now)
                self.store.settle_registration(conn, receipt.registration_ref, "ACK_ACCEPTED", now.isoformat())
                self._save(conn, order, hold)
                return {"status": "accepted", "order_id": order_id}
            # 释放后迟到的成功回执：归属不可回滚，恢复绑定并立案补救
            hold.rebind_after_late_registration(receipt.registration_ref, receipt.chain_tx_hash, now)
            case_id = self._id("CASE")
            case = RemedyCase.open(
                case_id,
                category=CASE_LATE_REGISTRATION,
                edition_id=order.edition_id,
                serial=order.serial,
                order_id=order.id,
                reason="链上登记成功回执在序号释放后到达，数字归属不可回滚",
                linked_case_ids=None,
                now=now,
            )
            order.enter_remedy(case_id, CASE_LATE_REGISTRATION, "迟到登记回执", now)
            self.store.settle_registration(conn, receipt.registration_ref, "ACK_ACCEPTED", now.isoformat())
            self._save(conn, order, hold, case)
            return {"status": "late_case", "order_id": order_id, "case_id": case_id}

        # 失败回执
        if order.status == ORDER_RELEASED:
            # 订单已释放（如在途时支付撤销）：失败回执只结算 outbox，不再产生领域事件
            self.store.settle_registration(conn, receipt.registration_ref, "ACK_FAILED", now.isoformat())
            return {"status": "ignored_released", "order_id": order_id}
        order.registration_failed(receipt.registration_ref, receipt.reason or "未知原因", receipt.retriable, now)
        if receipt.retriable:
            self.store.touch_registration_attempt(conn, receipt.registration_ref, receipt.reason, now.isoformat())
            self._save(conn, order)
            return {"status": "retry_pending", "order_id": order_id}
        hold.release(REL_CHAIN_FAILED, now)
        self.store.settle_registration(conn, receipt.registration_ref, "ACK_FAILED", now.isoformat())
        self._save(conn, order, hold)
        return {"status": "failed_released", "order_id": order_id}

    def _find_order_by_registration(self, registration_ref: str) -> str | None:
        for event in self.store.load_events_by_type(
            ev.AGG_PURCHASE_ORDER, (ev.EVENT_REGISTRATION_SUBMITTED,)
        ):
            if event.payload.get("registration_ref") == registration_ref:
                return event.aggregate_id
        return None

    # ------------------------------------------------------------------ #
    # 实体发运（同一归属；出库前地址变更需追加审批）
    # ------------------------------------------------------------------ #
    def create_shipment(self, order_id: str, recipient_name: str, address: str, phone: str) -> str:
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            order.create_shipment(recipient_name, address, phone, self.clock.now())
            self._save(conn, order)
            return order.shipment.shipment_no

    def request_address_change(self, order_id: str, new_address: str, requester: str) -> None:
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            order.request_address_change(new_address, requester, self.clock.now())
            self._save(conn, order)

    def approve_address_change(self, order_id: str, approver: str) -> None:
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            order.approve_address_change(approver, self.clock.now())
            self._save(conn, order)

    def reject_address_change(self, order_id: str, approver: str) -> None:
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            order.reject_address_change(approver, self.clock.now())
            self._save(conn, order)

    def dispatch(self, order_id: str, carrier: str, tracking_no: str) -> None:
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            order.dispatch(carrier, tracking_no, self.clock.now())
            self._save(conn, order)

    def deliver(self, order_id: str) -> None:
        with self.store.transaction() as conn:
            order = self._load_order(order_id)
            order.deliver(self.clock.now())
            self._save(conn, order)

    # ------------------------------------------------------------------ #
    # 物流退回（inbox，重启后继续处理）
    # ------------------------------------------------------------------ #
    def receive_return(self, return_id: str, shipment_no: str, reason: str) -> bool:
        """登记物流退回回执；同一 return_id 重放返回 False 且不重复处理。"""
        now = self.clock.now()
        with self.store.transaction() as conn:
            is_new = self.store.receive_return(conn, return_id, shipment_no, reason, now.isoformat())
        if is_new:
            self._process_return(return_id)
        return is_new

    def _process_return(self, return_id: str) -> None:
        now = self.clock.now()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM return_inbox WHERE return_id=?", (return_id,)
            ).fetchone()
            if row is None or row["state"] != "RECEIVED":
                return
            order_id = self._find_order_by_shipment(row["shipment_no"])
            if order_id is None:
                raise FulfillmentError(f"找不到发运 {row['shipment_no']} 对应的订单")
            order = self._load_order(order_id)
            if order.shipment is None or order.shipment.status == "RETURNED":
                self.store.mark_return_processed(conn, return_id)
                return
            case_id = self._id("CASE")
            case = RemedyCase.open(
                case_id,
                category=CASE_SHIPMENT_RETURN,
                edition_id=order.edition_id,
                serial=order.serial,
                order_id=order.id,
                reason=row["reason"],
                linked_case_ids=None,
                now=now,
            )
            order.returned(case_id, row["reason"], now)
            self.store.mark_return_processed(conn, return_id)
            self._save(conn, order, case)

    def _find_order_by_shipment(self, shipment_no: str) -> str | None:
        for event in self.store.load_events_by_type(
            ev.AGG_PURCHASE_ORDER, (ev.EVENT_SHIPMENT_TASK_CREATED,)
        ):
            if event.payload.get("shipment_no") == shipment_no:
                return event.aggregate_id
        return None

    # ------------------------------------------------------------------ #
    # 补救案件与内容隔离
    # ------------------------------------------------------------------ #
    def open_content_isolation(
        self,
        edition_id: str,
        serial: int,
        reason: str,
        order_id: str | None = None,
        linked_case_ids: list[str] | None = None,
    ) -> str:
        """内容变化隔离调查：立案并暂停关联订单的后续履约。"""
        now = self.clock.now()
        with self.store.transaction() as conn:
            case_id = self._id("CASE")
            case = RemedyCase.open(
                case_id,
                category=CASE_CONTENT_ISOLATION,
                edition_id=edition_id,
                serial=serial,
                order_id=order_id,
                reason=reason,
                linked_case_ids=linked_case_ids,
                now=now,
            )
            aggregates: list = [case]
            if order_id:
                order = self._load_order(order_id)
                order.quarantine(case_id, reason, now)
                aggregates.append(order)
            self._save(conn, *aggregates)
        return case_id

    def resolve_case(self, case_id: str, resolution: str) -> None:
        now = self.clock.now()
        with self.store.transaction() as conn:
            case = self._load_case(case_id)
            case.resolve(resolution, now)
            aggregates: list = [case]
            if case.category == CASE_CONTENT_ISOLATION and case.order_id:
                order = self._load_order(case.order_id)
                order.clear_quarantine(case_id, now)
                aggregates.append(order)
            self._save(conn, *aggregates)

    def _isolate_for_receipt_conflict(
        self,
        hold_id: str | None = None,
        registration_ref: str | None = None,
        receipt: str = "",
    ) -> None:
        """回执内容变化：对相关序号立案隔离（尽力而为，不遮蔽原始异常）。"""
        try:
            edition_id = serial = None
            order_id = None
            if hold_id:
                hold = self._load_hold(hold_id)
                edition_id, serial, order_id = hold.edition_id, hold.serial, hold.order_id
            elif registration_ref:
                order_id = self._find_order_by_registration(registration_ref)
                if order_id:
                    order = self._load_order(order_id)
                    edition_id, serial = order.edition_id, order.serial
            if edition_id is not None:
                self.open_content_isolation(
                    edition_id, serial, f"回执 {receipt} 内容变化，隔离调查", order_id=order_id
                )
        except FulfillmentError:
            pass

    # ------------------------------------------------------------------ #
    # 重启恢复：过期释放、补登记、退件处理
    # ------------------------------------------------------------------ #
    def recover(self) -> dict[str, int]:
        """服务启动/定时调用：继续过期释放、补登记与退件处理。"""
        now = self.clock.now()
        expired = 0
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT hold_id FROM serial_slots WHERE state='HELD'"
            ).fetchall()
            holds = [self._load_hold(r["hold_id"]) for r in rows]
            due = [h for h in holds if h.expire_if_due(now)]
            if due:
                self._save(conn, *due)
            expired = len(due)

        driven = 0
        for row in self.store.pending_registrations():
            self._drive_registration(row["registration_ref"])
            driven += 1

        returned = 0
        for row in self.store.pending_returns():
            self._process_return(row["return_id"])
            returned += 1

        return {"expired_holds": expired, "registrations_driven": driven, "returns_processed": returned}

    # ------------------------------------------------------------------ #
    # 查询：从任一序号还原全链路依据
    # ------------------------------------------------------------------ #
    def trace_serial(self, edition_id: str, serial: int) -> dict[str, Any]:
        """按序号还原预占、付款、登记、发运与补救依据。"""
        holds = []
        orders = []
        cases = []
        for event in self.store.all_events():
            p = event.payload
            if event.aggregate_type == ev.AGG_SERIAL_RESERVATION and p.get("serial") == serial \
                    and p.get("edition_id") == edition_id:
                holds.append(event)
            elif event.aggregate_type == ev.AGG_PURCHASE_ORDER and p.get("serial") == serial \
                    and p.get("edition_id") == edition_id:
                orders.append(event)
            elif event.aggregate_type == ev.AGG_FULFILLMENT_CASE and p.get("serial") == serial \
                    and p.get("edition_id") == edition_id:
                cases.append(event)

        order_ids = {e.aggregate_id for e in orders}
        order_events = [e for e in self.store.all_events() if e.aggregate_id in order_ids]
        case_ids = {e.aggregate_id for e in cases}
        case_events = [e for e in self.store.all_events() if e.aggregate_id in case_ids]

        slot = self.store.read_slot(edition_id, serial)

        def brief(e: ev.Event) -> dict[str, Any]:
            return {
                "event_type": e.event_type,
                "aggregate_id": e.aggregate_id,
                "occurred_at": e.occurred_at.isoformat(),
                "version": e.version,
                "summary": e.summary,
                "payload": e.payload,
            }

        return {
            "edition_id": edition_id,
            "serial": serial,
            "slot": slot,
            "reservation": [brief(e) for e in sorted(holds, key=lambda x: (x.aggregate_id, x.version))],
            "payment": [brief(e) for e in order_events
                        if e.event_type in (ev.EVENT_PAYMENT_CONFIRMED, ev.EVENT_PAYMENT_REVOKED)],
            "registration": [brief(e) for e in order_events
                             if e.event_type in (ev.EVENT_REGISTRATION_SUBMITTED,
                                                 ev.EVENT_REGISTRATION_ACCEPTED,
                                                 ev.EVENT_REGISTRATION_FAILED)],
            "shipment": [brief(e) for e in order_events
                         if e.event_type in (ev.EVENT_SHIPMENT_TASK_CREATED,
                                             ev.EVENT_ADDRESS_CHANGE_REQUESTED,
                                             ev.EVENT_ADDRESS_CHANGE_APPROVED,
                                             ev.EVENT_ADDRESS_CHANGE_REJECTED,
                                             ev.EVENT_PHYSICAL_DISPATCHED,
                                             ev.EVENT_SHIPMENT_DELIVERED,
                                             ev.EVENT_SHIPMENT_RETURNED)],
            "remedy": [brief(e) for e in sorted(case_events + [
                e for e in order_events if e.event_type in (
                    ev.EVENT_ORDER_REMEDIED, ev.EVENT_ORDER_QUARANTINED, ev.EVENT_ORDER_QUARANTINE_CLEARED)
            ], key=lambda x: x.occurred_at)],
        }
