"""领域聚合与状态机。

聚合只做纯领域决策：接收命令、校验状态、产生事件；不触碰数据库与时钟实现
（当前时间由命令参数注入）。四个聚合：

- ``Edition`` 版次：登记总量与保留范围（不售序号）。
- ``SerialReservation`` 序号预占：可过期的占位，付款后绑定订单，登记成功确认归属，
  未完成前可被释放（释放后允许再次发售）。
- ``PurchaseOrder`` 订单：付款只锁定购买资格；实名复核与链上登记通过后才确认归属；
  归属确认后按同一归属生成实体发运任务，出库前地址变更需追加审批。
- ``RemedyCase`` 补救案件：已登记数字归属不可回滚，退回/迟到登记/内容问题只能立案补救。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import events as ev
from .errors import (
    AddressChangePending,
    CaseError,
    HoldExpired,
    IllegalTransition,
    RegistrationIrreversible,
    SerialUnavailable,
    ShipmentNotOutbound,
)

# ---- 预占/绑定状态 ----
HOLD_HELD = "HELD"
HOLD_LINKED = "LINKED"
HOLD_RELEASED = "RELEASED"
HOLD_REGISTERED = "REGISTERED"

# ---- 订单状态 ----
ORDER_PAID = "PAID"
ORDER_ID_VERIFIED = "ID_VERIFIED"
ORDER_REGISTERING = "REGISTERING"
ORDER_REGISTERED = "REGISTERED"
ORDER_DISPATCHED = "DISPATCHED"
ORDER_DELIVERED = "DELIVERED"
ORDER_RELEASED = "RELEASED"
ORDER_REMEDY = "REMEDY"

# ---- 释放原因 ----
REL_EXPIRED = "hold_expired"
REL_PAYMENT_REVOKED = "payment_revoked"
REL_IDENTITY_REJECTED = "identity_rejected"
REL_CHAIN_FAILED = "chain_failed"

# ---- 发运状态 ----
SHIP_PENDING = "PENDING"
SHIP_DISPATCHED = "DISPATCHED"
SHIP_DELIVERED = "DELIVERED"
SHIP_RETURNED = "RETURNED"

# ---- 补救类型 ----
CASE_SHIPMENT_RETURN = "SHIPMENT_RETURN"
CASE_CONTENT_ISOLATION = "CONTENT_ISOLATION"
CASE_LATE_REGISTRATION = "LATE_REGISTRATION"

CASE_OPEN = "OPEN"
CASE_RESOLVED = "RESOLVED"


class Aggregate:
    aggregate_type = ""

    def __init__(self, aggregate_id: str) -> None:
        self.id = aggregate_id
        self.version = 0
        self._new_events: list[ev.Event] = []

    def _raise(
        self,
        event_type: str,
        now: datetime,
        payload: dict[str, Any],
        summary: str,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        causation_id: str | None = None,
    ) -> ev.Event:
        event = ev.Event(
            event_id=None,
            event_type=event_type,
            aggregate_type=aggregate_type or self.aggregate_type,
            aggregate_id=aggregate_id or self.id,
            occurred_at=now,
            version=self.version + 1,
            summary=summary,
            payload=payload,
            causation_id=causation_id,
        )
        self.apply(event)
        self.version = event.version
        self._new_events.append(event)
        return event

    def pull_events(self) -> list[ev.Event]:
        pending = self._new_events
        self._new_events = []
        return pending

    @staticmethod
    def _load(cls: type["Aggregate"], aggregate_id: str, history: list[ev.Event]) -> "Aggregate":
        agg = cls(aggregate_id)
        for event in history:
            agg.apply(event)
            agg.version = event.version
        agg._new_events = []
        return agg


# --------------------------------------------------------------------------- #
# 版次
# --------------------------------------------------------------------------- #
@dataclass
class ReservedRange:
    low: int
    high: int

    def contains(self, serial: int) -> bool:
        return self.low <= serial <= self.high


class Edition(Aggregate):
    aggregate_type = ev.AGG_EDITION

    def __init__(self, edition_id: str) -> None:
        super().__init__(edition_id)
        self.title: str | None = None
        self.total_serials = 0
        self.reserved_ranges: list[ReservedRange] = []

    @classmethod
    def register(
        cls,
        edition_id: str,
        title: str,
        total_serials: int,
        reserved_ranges: list[tuple[int, int]] | None,
        now: datetime,
    ) -> "Edition":
        if total_serials < 1:
            raise IllegalTransition("版次总量必须为正整数")
        ranges = []
        for low, high in sorted(reserved_ranges or []):
            if low < 1 or high > total_serials or low > high:
                raise IllegalTransition(f"保留范围超出版次边界：{low}-{high}")
            if ranges and low <= ranges[-1].high:
                raise IllegalTransition(f"保留范围重叠：{low}-{high}")
            ranges.append(ReservedRange(low, high))
        edition = cls(edition_id)
        edition._raise(
            ev.EVENT_EDITION_REGISTERED,
            now,
            {
                "title": title,
                "total_serials": total_serials,
                "reserved_ranges": [[r.low, r.high] for r in ranges],
            },
            f"版次《{title}》登记总量 {total_serials}，保留 {len(ranges)} 段序号",
        )
        return edition

    @classmethod
    def load(cls, edition_id: str, history: list[ev.Event]) -> "Edition":
        return Aggregate._load(cls, edition_id, history)

    def is_reserved(self, serial: int) -> bool:
        return any(r.contains(serial) for r in self.reserved_ranges)

    def assert_sellable(self, serial: int) -> None:
        if serial < 1 or serial > self.total_serials:
            raise SerialUnavailable(f"序号 {serial} 超出版次总量 {self.total_serials}")
        if self.is_reserved(serial):
            raise SerialUnavailable(f"序号 {serial} 处于版次保留范围")

    def sellable_serials(self) -> list[int]:
        return [s for s in range(1, self.total_serials + 1) if not self.is_reserved(s)]

    def apply(self, event: ev.Event) -> None:
        if event.event_type == ev.EVENT_EDITION_REGISTERED:
            self.title = event.payload["title"]
            self.total_serials = event.payload["total_serials"]
            self.reserved_ranges = [ReservedRange(a, b) for a, b in event.payload["reserved_ranges"]]


# --------------------------------------------------------------------------- #
# 序号预占
# --------------------------------------------------------------------------- #
class SerialReservation(Aggregate):
    aggregate_type = ev.AGG_SERIAL_RESERVATION

    def __init__(self, hold_id: str) -> None:
        super().__init__(hold_id)
        self.edition_id = ""
        self.serial = 0
        self.buyer_ref = ""
        self.expires_at: datetime | None = None
        self.state = ""
        self.order_id: str | None = None
        self.payment_ref: str | None = None
        self.release_reason: str | None = None
        self.registration_ref: str | None = None

    @classmethod
    def create(
        cls,
        hold_id: str,
        edition_id: str,
        serial: int,
        buyer_ref: str,
        expires_at: datetime,
        now: datetime,
    ) -> "SerialReservation":
        hold = cls(hold_id)
        hold._raise(
            ev.EVENT_SERIAL_HELD,
            now,
            {
                "edition_id": edition_id,
                "serial": serial,
                "buyer_ref": buyer_ref,
                "expires_at": expires_at.isoformat(),
            },
            f"预占序号 {serial}，将于 {expires_at.isoformat()} 过期",
        )
        return hold

    @classmethod
    def load(cls, hold_id: str, history: list[ev.Event]) -> "SerialReservation":
        return Aggregate._load(cls, hold_id, history)

    def expire_if_due(self, now: datetime) -> bool:
        """到期判定：now >= expires_at 的临界时刻即过期。"""
        if self.state != HOLD_HELD:
            return False
        if now < self.expires_at:
            return False
        self._raise(
            ev.EVENT_SERIAL_HOLD_EXPIRED,
            now,
            {"edition_id": self.edition_id, "serial": self.serial, "release_reason": REL_EXPIRED},
            f"序号 {self.serial} 预占过期，释放待售",
        )
        self._release(REL_EXPIRED, now, emit=False)
        return True

    def assert_active(self, now: datetime) -> None:
        if self.state == HOLD_HELD and now >= self.expires_at:
            raise HoldExpired(f"序号 {self.serial} 预占已过期")
        if self.state != HOLD_HELD:
            raise SerialUnavailable(f"序号 {self.serial} 预占状态为 {self.state}")

    def link_order(self, order_id: str, payment_ref: str, now: datetime) -> None:
        if self.state != HOLD_HELD:
            raise IllegalTransition(f"序号 {self.serial} 预占状态为 {self.state}，不能绑定付款")
        if now >= self.expires_at:
            raise HoldExpired(f"序号 {self.serial} 预占恰在付款时过期")
        self._raise(
            ev.EVENT_SERIAL_RESERVATION_LINKED,
            now,
            {"edition_id": self.edition_id, "serial": self.serial, "order_id": order_id, "payment_ref": payment_ref},
            f"序号 {self.serial} 付款锁定购买资格，绑定订单 {order_id}",
        )

    def release(self, reason: str, now: datetime) -> None:
        if self.state == HOLD_REGISTERED:
            raise RegistrationIrreversible(f"序号 {self.serial} 已完成链上登记，不可释放")
        if self.state == HOLD_RELEASED:
            return
        if self.state != HOLD_LINKED:
            raise IllegalTransition(f"序号 {self.serial} 状态为 {self.state}，不在可释放阶段")
        self._raise(
            ev.EVENT_SERIAL_RESERVATION_RELEASED,
            now,
            {"edition_id": self.edition_id, "serial": self.serial, "release_reason": reason},
            f"序号 {self.serial} 因 {reason} 释放，重新可售",
        )
        self.state = HOLD_RELEASED
        self.release_reason = reason

    def _release(self, reason: str, now: datetime, emit: bool) -> None:
        if emit:
            self.release(reason, now)
            return
        self.state = HOLD_RELEASED
        self.release_reason = reason

    def mark_registered(self, registration_ref: str, chain_tx_hash: str, now: datetime) -> None:
        if self.state == HOLD_REGISTERED:
            return
        if self.state != HOLD_LINKED:
            raise IllegalTransition(f"序号 {self.serial} 状态为 {self.state}，不能确认归属")
        self._raise(
            ev.EVENT_SERIAL_OWNERSHIP_REGISTERED,
            now,
            {
                "edition_id": self.edition_id,
                "serial": self.serial,
                "order_id": self.order_id,
                "registration_ref": registration_ref,
                "chain_tx_hash": chain_tx_hash,
            },
            f"序号 {self.serial} 链上归属确认（交易 {chain_tx_hash}）",
        )

    def rebind_after_late_registration(self, registration_ref: str, chain_tx_hash: str, now: datetime) -> None:
        """迟到的登记回执命中已释放序号：数字归属仍然成立，重新绑定并转补救。"""
        if self.state == HOLD_RELEASED:
            self._raise(
                ev.EVENT_SERIAL_OWNERSHIP_REGISTERED,
                now,
                {
                    "edition_id": self.edition_id,
                    "serial": self.serial,
                    "order_id": self.order_id,
                    "registration_ref": registration_ref,
                    "chain_tx_hash": chain_tx_hash,
                    "late": True,
                },
                f"序号 {self.serial} 释放后收到成功登记回执，恢复归属绑定并转补救",
            )
        else:
            self.mark_registered(registration_ref, chain_tx_hash, now)

    def apply(self, event: ev.Event) -> None:
        p = event.payload
        if event.event_type == ev.EVENT_SERIAL_HELD:
            self.edition_id = p["edition_id"]
            self.serial = p["serial"]
            self.buyer_ref = p["buyer_ref"]
            self.expires_at = datetime.fromisoformat(p["expires_at"])
            self.state = HOLD_HELD
        elif event.event_type == ev.EVENT_SERIAL_HOLD_EXPIRED:
            self.state = HOLD_RELEASED
            self.release_reason = REL_EXPIRED
        elif event.event_type == ev.EVENT_SERIAL_RESERVATION_LINKED:
            self.state = HOLD_LINKED
            self.order_id = p["order_id"]
            self.payment_ref = p["payment_ref"]
        elif event.event_type == ev.EVENT_SERIAL_RESERVATION_RELEASED:
            self.state = HOLD_RELEASED
            self.release_reason = p["release_reason"]
        elif event.event_type == ev.EVENT_SERIAL_OWNERSHIP_REGISTERED:
            self.state = HOLD_REGISTERED
            self.registration_ref = p["registration_ref"]
            self.order_id = p.get("order_id", self.order_id)


# --------------------------------------------------------------------------- #
# 订单
# --------------------------------------------------------------------------- #
@dataclass
class ShipmentTask:
    shipment_no: str
    recipient_name: str
    address: str
    phone: str
    status: str = SHIP_PENDING
    carrier: str | None = None
    tracking_no: str | None = None
    dispatched_at: datetime | None = None
    pending_address: dict[str, Any] | None = None


class PurchaseOrder(Aggregate):
    aggregate_type = ev.AGG_PURCHASE_ORDER

    def __init__(self, order_id: str) -> None:
        super().__init__(order_id)
        self.edition_id = ""
        self.serial = 0
        self.hold_id = ""
        self.buyer_ref = ""
        self.payment_ref: str | None = None
        self.amount: int | None = None
        self.status = ""
        self.identity: dict[str, Any] | None = None
        self.registration_ref: str | None = None
        self.chain_tx_hash: str | None = None
        self.shipment: ShipmentTask | None = None
        self.quarantined = False
        self.case_id: str | None = None
        self.release_reason: str | None = None
        self.late_registration = False

    @classmethod
    def create(
        cls,
        order_id: str,
        *,
        hold_id: str,
        edition_id: str,
        serial: int,
        buyer_ref: str,
        payment_ref: str,
        amount: int,
        now: datetime,
    ) -> "PurchaseOrder":
        if amount < 0:
            raise IllegalTransition("支付金额不能为负")
        order = cls(order_id)
        order._raise(
            ev.EVENT_PAYMENT_CONFIRMED,
            now,
            {
                "hold_id": hold_id,
                "edition_id": edition_id,
                "serial": serial,
                "buyer_ref": buyer_ref,
                "payment_ref": payment_ref,
                "amount": amount,
            },
            f"订单 {order_id} 支付回执 {payment_ref} 确认，锁定序号 {serial} 购买资格",
        )
        return order

    @classmethod
    def load(cls, order_id: str, history: list[ev.Event]) -> "PurchaseOrder":
        return Aggregate._load(cls, order_id, history)

    # -- 支付 -------------------------------------------------------------- #
    def revoke_payment(self, now: datetime, reason: str) -> None:
        if self.status == ORDER_REGISTERED:
            raise RegistrationIrreversible("数字归属已登记，支付撤销不能回滚归属，请走补救案件")
        if self.status == ORDER_RELEASED:
            return
        if self.status == ORDER_REMEDY:
            raise RegistrationIrreversible("订单已在补救案件中，不能按支付撤销处理")
        # REGISTERING 中允许撤销：释放在途资源；若链上稍后成功，按迟到回执转补救案件
        self._raise(
            ev.EVENT_PAYMENT_REVOKED,
            now,
            {"payment_ref": self.payment_ref, "release_reason": REL_PAYMENT_REVOKED, "reason": reason},
            f"订单 {self.id} 支付撤销，释放序号 {self.serial}",
        )

    # -- 实名 -------------------------------------------------------------- #
    def pass_identity(self, materials: dict[str, Any], now: datetime) -> None:
        if self.status != ORDER_PAID:
            raise IllegalTransition(f"订单状态 {self.status}，不能提交实名复核")
        for field_name in ("real_name", "document_ref"):
            if not materials.get(field_name):
                raise IllegalTransition(f"实名材料缺少 {field_name}")
        self._raise(
            ev.EVENT_IDENTITY_REVIEW_PASSED,
            now,
            {
                "real_name": materials["real_name"],
                "document_ref": materials["document_ref"],
                "reviewed_at": now.isoformat(),
            },
            f"订单 {self.id} 实名复核通过",
        )

    def reject_identity(self, reason: str, now: datetime) -> None:
        if self.status in (ORDER_RELEASED,):
            return
        if self.status not in (ORDER_PAID, ORDER_ID_VERIFIED):
            raise IllegalTransition(f"订单状态 {self.status}，不能退回实名材料")
        self._raise(
            ev.EVENT_IDENTITY_REVIEW_REJECTED,
            now,
            {"release_reason": REL_IDENTITY_REJECTED, "reason": reason},
            f"订单 {self.id} 实名复核未通过（{reason}），释放序号 {self.serial}",
        )

    # -- 链上登记 ---------------------------------------------------------- #
    def submit_registration(self, registration_ref: str, now: datetime) -> None:
        if self.status != ORDER_ID_VERIFIED:
            raise IllegalTransition(f"订单状态 {self.status}，暂不能提交链上登记")
        self._raise(
            ev.EVENT_REGISTRATION_SUBMITTED,
            now,
            {"registration_ref": registration_ref},
            f"订单 {self.id} 链上登记 {registration_ref} 已提交等待回执",
        )

    def registration_accepted(self, registration_ref: str, chain_tx_hash: str, now: datetime) -> bool:
        """返回 True 表示正常确认；False 表示释放后迟到的成功回执（需转补救案件）。"""
        if self.status == ORDER_REGISTERING:
            self._raise(
                ev.EVENT_REGISTRATION_ACCEPTED,
                now,
                {"registration_ref": registration_ref, "chain_tx_hash": chain_tx_hash},
                f"订单 {self.id} 链上登记成功，序号 {self.serial} 归属确认",
            )
            return True
        if self.status == ORDER_RELEASED:
            self._raise(
                ev.EVENT_REGISTRATION_ACCEPTED,
                now,
                {"registration_ref": registration_ref, "chain_tx_hash": chain_tx_hash, "late": True},
                f"订单 {self.id} 释放后迟到的登记成功回执，归属不可回滚，转补救案件",
            )
            return False
        raise IllegalTransition(f"订单状态 {self.status}，不能接受登记回执")

    def registration_failed(self, registration_ref: str, reason: str, retriable: bool, now: datetime) -> None:
        if self.status != ORDER_REGISTERING:
            return
        if retriable:
            self._raise(
                ev.EVENT_REGISTRATION_FAILED,
                now,
                {"registration_ref": registration_ref, "reason": reason, "retriable": True},
                f"订单 {self.id} 链上登记临时失败（{reason}），可重试",
            )
        else:
            self._raise(
                ev.EVENT_REGISTRATION_FAILED,
                now,
                {"registration_ref": registration_ref, "reason": reason, "retriable": False,
                 "release_reason": REL_CHAIN_FAILED},
                f"订单 {self.id} 链上登记终态失败（{reason}），释放序号 {self.serial}",
            )

    # -- 实体发运 ---------------------------------------------------------- #
    def create_shipment(self, recipient_name: str, address: str, phone: str, now: datetime) -> None:
        if self.status != ORDER_REGISTERED:
            raise IllegalTransition("只有数字归属确认后才能生成实体发运任务")
        if self.quarantined:
            raise IllegalTransition("订单处于内容隔离调查，暂缓实体发运")
        shipment_no = f"SHIP-{self.id}"
        self._raise(
            ev.EVENT_SHIPMENT_TASK_CREATED,
            now,
            {"shipment_no": shipment_no, "recipient_name": recipient_name, "address": address, "phone": phone},
            f"按序号 {self.serial} 同一归属生成发运任务 {shipment_no}",
        )

    def request_address_change(self, new_address: str, requester: str, now: datetime) -> None:
        if self.shipment is None:
            raise IllegalTransition("发运任务尚未生成")
        if self.shipment.status != SHIP_PENDING:
            raise ShipmentNotOutbound("实体已出库，地址不能变更")
        if self.shipment.pending_address is not None:
            raise AddressChangePending("已有待审批的地址变更")
        self._raise(
            ev.EVENT_ADDRESS_CHANGE_REQUESTED,
            now,
            {"shipment_no": self.shipment.shipment_no, "new_address": new_address, "requester": requester},
            f"发运 {self.shipment.shipment_no} 申请变更收件地址，等待出库前追加审批",
        )

    def approve_address_change(self, approver: str, now: datetime) -> None:
        if self.shipment is None or self.shipment.pending_address is None:
            raise IllegalTransition("没有待审批的地址变更")
        if self.shipment.status != SHIP_PENDING:
            raise ShipmentNotOutbound("实体已出库，地址变更审批自动失效")
        self._raise(
            ev.EVENT_ADDRESS_CHANGE_APPROVED,
            now,
            {"shipment_no": self.shipment.shipment_no, "approver": approver,
             "new_address": self.shipment.pending_address["new_address"]},
            f"地址变更经 {approver} 批准，发运 {self.shipment.shipment_no} 更新收件地址",
        )

    def reject_address_change(self, approver: str, now: datetime) -> None:
        if self.shipment is None or self.shipment.pending_address is None:
            raise IllegalTransition("没有待审批的地址变更")
        self._raise(
            ev.EVENT_ADDRESS_CHANGE_REJECTED,
            now,
            {"shipment_no": self.shipment.shipment_no, "approver": approver},
            f"地址变更被 {approver} 拒绝，沿用原地址",
        )

    def dispatch(self, carrier: str, tracking_no: str, now: datetime) -> None:
        if self.shipment is None:
            raise IllegalTransition("发运任务尚未生成")
        if self.quarantined:
            raise IllegalTransition("订单处于内容隔离调查，暂停出库")
        if self.shipment.status != SHIP_PENDING:
            raise IllegalTransition(f"发运状态 {self.shipment.status}，不能出库")
        if self.shipment.pending_address is not None:
            raise IllegalTransition("地址变更待审批，须先完成追加审批才能出库")
        self._raise(
            ev.EVENT_PHYSICAL_DISPATCHED,
            now,
            {"shipment_no": self.shipment.shipment_no, "carrier": carrier, "tracking_no": tracking_no},
            f"发运 {self.shipment.shipment_no} 已出库（{carrier}/{tracking_no}）",
        )

    def deliver(self, now: datetime) -> None:
        if self.status != ORDER_DISPATCHED:
            raise IllegalTransition(f"订单状态 {self.status}，不能确认签收")
        self._raise(
            ev.EVENT_SHIPMENT_DELIVERED,
            now,
            {"shipment_no": self.shipment.shipment_no},
            f"发运 {self.shipment.shipment_no} 已签收",
        )

    def returned(self, case_id: str, reason: str, now: datetime) -> None:
        """物流退回：数字归属不可回滚，订单转补救案件。"""
        if self.shipment is None or self.shipment.status not in (SHIP_DISPATCHED, SHIP_DELIVERED):
            raise IllegalTransition("只有在途/已签收的发运才会发生退回")
        if self.shipment.status == SHIP_RETURNED:
            return
        self._raise(
            ev.EVENT_SHIPMENT_RETURNED,
            now,
            {"shipment_no": self.shipment.shipment_no, "reason": reason, "case_id": case_id},
            f"发运 {self.shipment.shipment_no} 物流退回（{reason}），立案补救",
        )
        self._raise(
            ev.EVENT_ORDER_REMEDIED,
            now,
            {"case_category": CASE_SHIPMENT_RETURN, "case_id": case_id, "reason": reason},
            f"订单 {self.id} 因物流退回进入补救案件 {case_id}",
        )

    def quarantine(self, case_id: str, reason: str, now: datetime) -> None:
        if self.quarantined:
            return
        self._raise(
            ev.EVENT_ORDER_QUARANTINED,
            now,
            {"case_id": case_id, "reason": reason},
            f"订单 {self.id} 因内容变化隔离调查暂停履约",
        )

    def clear_quarantine(self, case_id: str, now: datetime) -> None:
        if not self.quarantined:
            return
        self._raise(
            ev.EVENT_ORDER_QUARANTINE_CLEARED,
            now,
            {"case_id": case_id},
            f"订单 {self.id} 隔离调查结束，恢复履约",
        )

    def enter_remedy(self, case_id: str, category: str, reason: str, now: datetime) -> None:
        if self.status == ORDER_REMEDY and self.case_id == case_id:
            return
        self._raise(
            ev.EVENT_ORDER_REMEDIED,
            now,
            {"case_category": category, "case_id": case_id, "reason": reason},
            f"订单 {self.id} 进入补救案件 {case_id}（{category}）",
        )

    # -- 投影 -------------------------------------------------------------- #
    def apply(self, event: ev.Event) -> None:
        p = event.payload
        t = event.event_type
        if t == ev.EVENT_PAYMENT_CONFIRMED:
            self.hold_id = p["hold_id"]
            self.edition_id = p["edition_id"]
            self.serial = p["serial"]
            self.buyer_ref = p["buyer_ref"]
            self.payment_ref = p["payment_ref"]
            self.amount = p["amount"]
            self.status = ORDER_PAID
        elif t == ev.EVENT_PAYMENT_REVOKED:
            self.status = ORDER_RELEASED
            self.release_reason = REL_PAYMENT_REVOKED
        elif t == ev.EVENT_IDENTITY_REVIEW_PASSED:
            self.identity = {"real_name": p["real_name"], "document_ref": p["document_ref"],
                             "reviewed_at": p["reviewed_at"]}
            self.status = ORDER_ID_VERIFIED
        elif t == ev.EVENT_IDENTITY_REVIEW_REJECTED:
            self.status = ORDER_RELEASED
            self.release_reason = REL_IDENTITY_REJECTED
        elif t == ev.EVENT_REGISTRATION_SUBMITTED:
            self.registration_ref = p["registration_ref"]
            self.status = ORDER_REGISTERING
        elif t == ev.EVENT_REGISTRATION_ACCEPTED:
            self.registration_ref = p["registration_ref"]
            self.chain_tx_hash = p["chain_tx_hash"]
            if p.get("late"):
                # 释放后迟到的成功回执：归属成立但订单保持待补救，由服务层立案追加 ORDER_REMEDIED
                self.late_registration = True
            else:
                self.status = ORDER_REGISTERED
        elif t == ev.EVENT_REGISTRATION_FAILED:
            if p.get("retriable"):
                self.status = ORDER_ID_VERIFIED
            else:
                self.status = ORDER_RELEASED
                self.release_reason = REL_CHAIN_FAILED
        elif t == ev.EVENT_SHIPMENT_TASK_CREATED:
            self.shipment = ShipmentTask(
                shipment_no=p["shipment_no"],
                recipient_name=p["recipient_name"],
                address=p["address"],
                phone=p["phone"],
            )
        elif t == ev.EVENT_ADDRESS_CHANGE_REQUESTED:
            self.shipment.pending_address = {"new_address": p["new_address"], "requester": p["requester"]}
        elif t == ev.EVENT_ADDRESS_CHANGE_APPROVED:
            self.shipment.address = p["new_address"]
            self.shipment.pending_address = None
        elif t == ev.EVENT_ADDRESS_CHANGE_REJECTED:
            self.shipment.pending_address = None
        elif t == ev.EVENT_PHYSICAL_DISPATCHED:
            self.shipment.status = SHIP_DISPATCHED
            self.shipment.carrier = p["carrier"]
            self.shipment.tracking_no = p["tracking_no"]
            self.shipment.dispatched_at = event.occurred_at
            self.status = ORDER_DISPATCHED
        elif t == ev.EVENT_SHIPMENT_DELIVERED:
            self.shipment.status = SHIP_DELIVERED
            self.status = ORDER_DELIVERED
        elif t == ev.EVENT_SHIPMENT_RETURNED:
            self.shipment.status = SHIP_RETURNED
        elif t == ev.EVENT_ORDER_QUARANTINED:
            self.quarantined = True
            self.case_id = p["case_id"]
        elif t == ev.EVENT_ORDER_QUARANTINE_CLEARED:
            self.quarantined = False
        elif t == ev.EVENT_ORDER_REMEDIED:
            self.status = ORDER_REMEDY
            self.case_id = p.get("case_id", self.case_id)


# --------------------------------------------------------------------------- #
# 补救案件
# --------------------------------------------------------------------------- #
class RemedyCase(Aggregate):
    aggregate_type = ev.AGG_FULFILLMENT_CASE

    def __init__(self, case_id: str) -> None:
        super().__init__(case_id)
        self.category = ""
        self.edition_id = ""
        self.serial: int | None = None
        self.order_id: str | None = None
        self.reason = ""
        self.linked_case_ids: list[str] = []
        self.status = ""
        self.resolution: str | None = None

    @classmethod
    def open(
        cls,
        case_id: str,
        *,
        category: str,
        edition_id: str,
        serial: int | None,
        order_id: str | None,
        reason: str,
        linked_case_ids: list[str] | None,
        now: datetime,
    ) -> "RemedyCase":
        case = cls(case_id)
        case._raise(
            ev.EVENT_REMEDY_CASE_OPENED,
            now,
            {
                "category": category,
                "edition_id": edition_id,
                "serial": serial,
                "order_id": order_id,
                "reason": reason,
                "linked_case_ids": linked_case_ids or [],
            },
            f"补救案件 {case_id} 立案（{category}）：{reason}",
        )
        return case

    @classmethod
    def load(cls, case_id: str, history: list[ev.Event]) -> "RemedyCase":
        return Aggregate._load(cls, case_id, history)

    def resolve(self, resolution: str, now: datetime) -> None:
        if self.status == CASE_RESOLVED:
            raise CaseError("案件已结案")
        self._raise(
            ev.EVENT_REMEDY_RESOLVED,
            now,
            {"resolution": resolution},
            f"补救案件 {self.id} 结案：{resolution}",
        )

    def apply(self, event: ev.Event) -> None:
        p = event.payload
        if event.event_type == ev.EVENT_REMEDY_CASE_OPENED:
            self.category = p["category"]
            self.edition_id = p["edition_id"]
            self.serial = p["serial"]
            self.order_id = p["order_id"]
            self.reason = p["reason"]
            self.linked_case_ids = list(p.get("linked_case_ids", []))
            self.status = CASE_OPEN
        elif event.event_type == ev.EVENT_REMEDY_RESOLVED:
            self.status = CASE_RESOLVED
            self.resolution = p["resolution"]
