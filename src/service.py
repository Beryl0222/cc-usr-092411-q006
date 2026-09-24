"""统一履约链服务。

履约链：版次登记（总量 + 保留范围 + 实体装裱额度）→ 序号预占（可注入时钟过期）
→ 付款锁定购买资格 → 实名复核 + 链上登记 → 确认数字归属 → 实体装裱发运
（出库前地址变更必须追加审批）。

释放规则：支付撤销、链上失败、物流退回分别释放尚未完成的资源；
已登记的数字归属不可回滚，只能进入补救案件。

幂等：同一支付或登记回执完全重放返回原结果；内容变化的回执隔离调查。

状态全部由事件日志重建；重启后调用 recover() 继续过期释放、补登记与退件处理。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .clock import Clock, SystemClock
from .models import Edition, Order, RemediationCase, Reservation, Shipment
from .store import EventStore

DEFAULT_HOLD_TTL = timedelta(minutes=30)


class FulfillmentError(Exception):
    """履约命令被拒绝。kind 供调用方分类处理。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class FulfillmentService:
    def __init__(
        self,
        store_path: str | Path,
        clock: Clock | None = None,
        hold_ttl: timedelta = DEFAULT_HOLD_TTL,
    ) -> None:
        self._clock = clock or SystemClock()
        self._hold_ttl = hold_ttl
        self._lock = threading.RLock()
        self._store = EventStore(store_path)
        self._editions: dict[str, Edition] = {}
        self._reservations: dict[str, Reservation] = {}
        self._orders: dict[str, Order] = {}
        self._shipments: dict[str, Shipment] = {}
        self._cases: dict[str, RemediationCase] = {}
        self._receipts: dict[str, dict] = {}  # 回执 id -> {"kind", "fingerprint", "order_id"}
        self._quarantine: list[dict] = []  # 内容变化的回执，隔离调查
        self._serial_reservation: dict[tuple[str, int], str] = {}  # (版次, 序号) -> 预占 id
        self._serial_order: dict[tuple[str, int], str] = {}  # (版次, 序号) -> 未释放订单 id
        self._order_shipment: dict[str, str] = {}  # 订单 id -> 发运 id
        self._order_cases: dict[str, list[str]] = {}  # 订单 id -> 补救案件 id 列表
        self._versions: dict[str, int] = {}
        self._seq = 0
        self._id_seq = 0
        for event in self._store.events:
            self._apply(event)
        # 事件序号与业务 id 序号都从日志长度继续，保证重启后不重复
        self._seq = len(self._store.events)
        self._id_seq = len(self._store.events)

    def close(self) -> None:
        self._store.close()

    # ------------------------------------------------------------------
    # 事件写入与回放
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str,
              summary: str, payload: dict) -> dict:
        self._seq += 1
        version = self._versions.get(aggregate_id, 0) + 1
        event = {
            "event_id": f"evt-{self._seq:08d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self._clock.now().isoformat(),
            "version": version,
            "summary": summary,
            "payload": payload,
        }
        self._versions[aggregate_id] = version
        self._store.append(event)
        self._apply(event)
        return event

    def _apply(self, event: dict) -> None:
        t = event["event_type"]
        p = event["payload"]
        agg = event["aggregate_id"]
        self._versions[agg] = event["version"]
        if t == "EDITION_REGISTERED":
            self._editions[p["edition_id"]] = Edition(
                edition_id=p["edition_id"],
                total_supply=p["total_supply"],
                reserved_ranges=[tuple(r) for r in p["reserved_ranges"]],
                physical_quota=p["physical_quota"],
            )
        elif t == "SERIAL_HELD":
            self._reservations[agg] = Reservation(
                reservation_id=agg,
                edition_id=p["edition_id"],
                serial_no=p["serial_no"],
                buyer_id=p["buyer_id"],
                expires_at=datetime.fromisoformat(p["expires_at"]),
            )
            self._serial_reservation[(p["edition_id"], p["serial_no"])] = agg
        elif t == "SERIAL_HOLD_EXPIRED":
            res = self._reservations[agg]
            res.status = "EXPIRED"
            self._serial_reservation.pop((res.edition_id, res.serial_no), None)
        elif t == "PAYMENT_CONFIRMED":
            res = self._reservations[p["reservation_id"]]
            res.status = "CONSUMED"
            res.order_id = agg
            self._serial_reservation.pop((res.edition_id, res.serial_no), None)
            self._orders[agg] = Order(
                order_id=agg,
                reservation_id=p["reservation_id"],
                edition_id=p["edition_id"],
                serial_no=p["serial_no"],
                buyer_id=p["buyer_id"],
                payment_id=p["payment_id"],
                amount=p["amount"],
                wants_physical=p["wants_physical"],
                address=p["address"],
                physical_earmarked=p["physical_earmarked"],
            )
            self._serial_order[(p["edition_id"], p["serial_no"])] = agg
            self._receipts[p["payment_id"]] = {
                "kind": "payment", "fingerprint": p["fingerprint"], "order_id": agg,
            }
        elif t == "PAYMENT_REVOKED":
            pass  # 审计依据；实际释放由 ORDER_RELEASED / REMEDIATION_OPENED 完成
        elif t == "REALNAME_APPROVED":
            self._orders[agg].realname = "APPROVED"
        elif t == "REALNAME_REJECTED":
            self._orders[agg].realname = "REJECTED"
        elif t == "REGISTRATION_SUBMITTED":
            self._orders[agg].registration = "SUBMITTED"
        elif t == "REGISTRATION_ACCEPTED":
            order = self._orders[agg]
            order.registration = "ACCEPTED"
            order.tx_hash = p["tx_hash"]
            order.registration_receipt_id = p["receipt_id"]
            self._receipts[p["receipt_id"]] = {
                "kind": "registration", "fingerprint": p["fingerprint"], "order_id": agg,
            }
        elif t == "REGISTRATION_FAILED":
            self._orders[agg].registration = "FAILED"
        elif t == "OWNERSHIP_CONFIRMED":
            self._orders[agg].status = "OWNERSHIP_CONFIRMED"
        elif t == "ORDER_RELEASED":
            order = self._orders[agg]
            order.status = "RELEASED"
            order.physical_earmarked = False
            self._serial_order.pop((order.edition_id, order.serial_no), None)
        elif t == "RECEIPT_QUARANTINED":
            self._quarantine.append({
                "event_id": event["event_id"],
                "order_id": p["order_id"],
                "receipt_id": p["receipt_id"],
                "kind": p["kind"],
                "expected_fingerprint": p["expected_fingerprint"],
                "received_fingerprint": p["received_fingerprint"],
                "received": p["received"],
                "edition_id": p["edition_id"],
                "serial_no": p["serial_no"],
            })
        elif t == "SHIPMENT_CREATED":
            self._shipments[agg] = Shipment(
                shipment_id=agg,
                order_id=p["order_id"],
                edition_id=p["edition_id"],
                serial_no=p["serial_no"],
                address=p["address"],
            )
            self._orders[p["order_id"]].physical_earmarked = False
            self._order_shipment[p["order_id"]] = agg
        elif t == "ADDRESS_CHANGE_REQUESTED":
            shp = self._shipments[agg]
            shp.status = "ADDRESS_CHANGE_PENDING"
            shp.pending_address = p["new_address"]
        elif t == "ADDRESS_CHANGE_APPROVED":
            shp = self._shipments[agg]
            shp.address = p["new_address"]
            shp.pending_address = None
            shp.status = "CREATED"
        elif t == "ADDRESS_CHANGE_REJECTED":
            shp = self._shipments[agg]
            shp.pending_address = None
            shp.status = "CREATED"
        elif t == "PHYSICAL_DISPATCHED":
            self._shipments[agg].status = "OUTBOUND"
        elif t == "SHIPMENT_DELIVERED":
            self._shipments[agg].status = "DELIVERED"
        elif t == "SHIPMENT_RETURNED":
            self._shipments[agg].status = "RETURNED"
        elif t == "SHIPMENT_RESTOCKED":
            self._shipments[agg].status = "RESTOCKED"
        elif t == "SHIPMENT_CANCELLED":
            self._shipments[agg].status = "CANCELLED"
        elif t == "REMEDIATION_OPENED":
            self._cases[agg] = RemediationCase(
                case_id=agg,
                order_id=p["order_id"],
                edition_id=p["edition_id"],
                serial_no=p["serial_no"],
                reason=p["reason"],
            )
            self._orders[p["order_id"]].in_remediation = True
            self._order_cases.setdefault(p["order_id"], []).append(agg)
        elif t == "ORDER_REMEDIED":
            case = self._cases[agg]
            case.status = "RESOLVED"
            case.resolution = p["resolution"]
            order = self._orders[case.order_id]
            order.in_remediation = any(
                self._cases[c].status == "OPEN" for c in self._order_cases.get(case.order_id, [])
            )

    def _next_id(self, prefix: str) -> str:
        self._id_seq += 1
        return f"{prefix}-{self._id_seq:06d}"

    @staticmethod
    def _fingerprint(payload: dict) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # 版次与序号预占
    # ------------------------------------------------------------------

    def register_edition(self, edition_id: str, total_supply: int,
                         reserved_ranges: list[tuple[int, int]] | None = None,
                         physical_quota: int = 0) -> dict:
        """登记版次总量、保留范围与实体装裱额度。"""
        with self._lock:
            if edition_id in self._editions:
                raise FulfillmentError("DUPLICATE_EDITION", f"版次已登记：{edition_id}")
            if total_supply < 1:
                raise FulfillmentError("INVALID_STATE", "总量必须为正整数")
            ranges = [tuple(r) for r in (reserved_ranges or [])]
            for start, end in ranges:
                if not (1 <= start <= end <= total_supply):
                    raise FulfillmentError("INVALID_STATE", f"保留范围越界：[{start}, {end}]")
            if physical_quota < 0:
                raise FulfillmentError("INVALID_STATE", "实体装裱额度不能为负")
            self._emit(
                "EDITION_REGISTERED", "edition", edition_id,
                f"登记版次 {edition_id}：总量 {total_supply}，保留 {len(ranges)} 段，实体额度 {physical_quota}",
                {
                    "edition_id": edition_id,
                    "total_supply": total_supply,
                    "reserved_ranges": [list(r) for r in ranges],
                    "physical_quota": physical_quota,
                },
            )
            return {"edition_id": edition_id, "total_supply": total_supply,
                    "reserved_ranges": ranges, "physical_quota": physical_quota}

    def hold_serial(self, edition_id: str, buyer_id: str, serial_no: int | None = None) -> dict:
        """预占序号；不指定序号时自动分配最小可用序号。预占随时钟过期。"""
        with self._lock:
            edition = self._require_edition(edition_id)
            now = self._clock.now()
            if serial_no is None:
                serial_no = self._first_available(edition, now)
                if serial_no is None:
                    raise FulfillmentError("NO_SERIAL_AVAILABLE", f"版次 {edition_id} 已无可售序号")
            else:
                self._check_serial_available(edition, serial_no, now)
            reservation_id = self._next_id("rsv")
            expires_at = now + self._hold_ttl
            self._emit(
                "SERIAL_HELD", "serial_reservation", reservation_id,
                f"买家 {buyer_id} 预占序号 {serial_no}，{expires_at.isoformat()} 过期",
                {
                    "reservation_id": reservation_id,
                    "edition_id": edition_id,
                    "serial_no": serial_no,
                    "buyer_id": buyer_id,
                    "expires_at": expires_at.isoformat(),
                },
            )
            return {"reservation_id": reservation_id, "serial_no": serial_no,
                    "expires_at": expires_at.isoformat()}

    def sweep_expired_holds(self, now: datetime | None = None) -> list[str]:
        """释放已过期预占（expires_at <= now 即过期，含临界时刻）。"""
        with self._lock:
            now = now or self._clock.now()
            expired = []
            for res in list(self._reservations.values()):
                if res.status == "HELD" and res.expires_at <= now:
                    self._emit(
                        "SERIAL_HOLD_EXPIRED", "serial_reservation", res.reservation_id,
                        f"预占过期，释放序号 {res.serial_no}",
                        {
                            "reservation_id": res.reservation_id,
                            "edition_id": res.edition_id,
                            "serial_no": res.serial_no,
                        },
                    )
                    expired.append(res.reservation_id)
            return expired

    # ------------------------------------------------------------------
    # 付款、实名复核与链上登记
    # ------------------------------------------------------------------

    def confirm_payment(self, reservation_id: str, payment_id: str, amount: int,
                        wants_physical: bool = False, address: str | None = None) -> dict:
        """支付回执确认：只锁定购买资格。同一回执重放返回原结果，内容变化隔离调查。"""
        with self._lock:
            fingerprint = self._fingerprint({
                "payment_id": payment_id,
                "reservation_id": reservation_id,
                "amount": amount,
                "wants_physical": bool(wants_physical),
                "address": address,
            })
            existing = self._receipts.get(payment_id)
            if existing is not None:
                if existing["fingerprint"] == fingerprint:
                    order = self._orders[existing["order_id"]]
                    return {"status": "REPLAYED", "order_id": order.order_id,
                            "payment_id": payment_id, "serial_no": order.serial_no}
                return self._quarantine_receipt(existing, payment_id, "payment", fingerprint, {
                    "reservation_id": reservation_id, "amount": amount,
                    "wants_physical": bool(wants_physical), "address": address,
                })
            res = self._reservations.get(reservation_id)
            if res is None:
                raise FulfillmentError("NOT_FOUND", f"预占不存在：{reservation_id}")
            if res.status != "HELD":
                raise FulfillmentError("INVALID_STATE", f"预占 {reservation_id} 状态为 {res.status}")
            now = self._clock.now()
            if res.expires_at <= now:
                raise FulfillmentError("HOLD_EXPIRED",
                                       f"预占 {reservation_id} 已于 {res.expires_at.isoformat()} 过期")
            if wants_physical and not address:
                raise FulfillmentError("INVALID_STATE", "实体装裱必须提供收件地址")
            earmarked = False
            if wants_physical:
                edition = self._editions[res.edition_id]
                if self._physical_committed(res.edition_id) >= edition.physical_quota:
                    raise FulfillmentError("QUOTA_EXHAUSTED", "实体装裱额度已用完")
                earmarked = True
            order_id = self._next_id("ord")
            self._emit(
                "PAYMENT_CONFIRMED", "purchase_order", order_id,
                f"支付确认，锁定序号 {res.serial_no} 的购买资格",
                {
                    "order_id": order_id,
                    "reservation_id": reservation_id,
                    "payment_id": payment_id,
                    "fingerprint": fingerprint,
                    "edition_id": res.edition_id,
                    "serial_no": res.serial_no,
                    "buyer_id": res.buyer_id,
                    "amount": amount,
                    "wants_physical": bool(wants_physical),
                    "physical_earmarked": earmarked,
                    "address": address,
                },
            )
            return {"status": "CONFIRMED", "order_id": order_id, "payment_id": payment_id,
                    "serial_no": res.serial_no, "physical_earmarked": earmarked}

    def revoke_payment(self, order_id: str, reason: str) -> dict:
        """支付撤销：归属未确认则释放序号与实体额度；已确认则数字归属不可回滚，进入补救。"""
        with self._lock:
            order = self._require_order(order_id)
            if order.status == "RELEASED":
                raise FulfillmentError("INVALID_STATE", f"订单 {order_id} 已释放")
            ownership = order.status == "OWNERSHIP_CONFIRMED"
            self._emit(
                "PAYMENT_REVOKED", "purchase_order", order_id,
                f"支付撤销：{reason}",
                {
                    "order_id": order_id,
                    "reason": reason,
                    "ownership_confirmed": ownership,
                    "edition_id": order.edition_id,
                    "serial_no": order.serial_no,
                },
            )
            if not ownership:
                self._release_order(order, "PAYMENT_REVOKED",
                                    f"支付撤销，释放序号 {order.serial_no} 与未履约资源")
                return {"status": "RELEASED", "order_id": order_id}
            shipment_id = self._order_shipment.get(order_id)
            if shipment_id and self._shipments[shipment_id].status in ("CREATED", "ADDRESS_CHANGE_PENDING"):
                self._emit(
                    "SHIPMENT_CANCELLED", "shipment", shipment_id,
                    "支付撤销，取消未出库发运任务",
                    {
                        "shipment_id": shipment_id,
                        "order_id": order_id,
                        "reason": "PAYMENT_REVOKED",
                        "edition_id": order.edition_id,
                        "serial_no": order.serial_no,
                    },
                )
            case_id = self._open_case(order, "PAYMENT_REVOKED_AFTER_OWNERSHIP")
            return {"status": "REMEDIATION", "order_id": order_id, "case_id": case_id}

    def approve_realname(self, order_id: str, reviewer: str) -> dict:
        with self._lock:
            order = self._require_order(order_id)
            self._require_active(order)
            if order.realname != "PENDING":
                raise FulfillmentError("INVALID_STATE", f"实名复核已完成：{order.realname}")
            self._emit(
                "REALNAME_APPROVED", "purchase_order", order_id,
                f"实名复核通过（{reviewer}）",
                {"order_id": order_id, "reviewer": reviewer,
                 "edition_id": order.edition_id, "serial_no": order.serial_no},
            )
            if order.registration == "ACCEPTED":
                self._confirm_ownership(order)
            return {"status": "APPROVED", "order_id": order_id, "ownership": order.status}

    def reject_realname(self, order_id: str, reason: str) -> dict:
        with self._lock:
            order = self._require_order(order_id)
            self._require_active(order)
            if order.realname != "PENDING":
                raise FulfillmentError("INVALID_STATE", f"实名复核已完成：{order.realname}")
            self._emit(
                "REALNAME_REJECTED", "purchase_order", order_id,
                f"实名复核驳回：{reason}",
                {"order_id": order_id, "reason": reason,
                 "edition_id": order.edition_id, "serial_no": order.serial_no},
            )
            self._release_order(order, "REALNAME_REJECTED",
                                f"实名复核驳回，释放序号 {order.serial_no} 与未履约资源")
            return {"status": "RELEASED", "order_id": order_id}

    def submit_registration(self, order_id: str) -> dict:
        with self._lock:
            order = self._require_order(order_id)
            self._require_active(order)
            if order.registration != "PENDING":
                raise FulfillmentError("INVALID_STATE", f"登记状态为 {order.registration}")
            self._emit(
                "REGISTRATION_SUBMITTED", "purchase_order", order_id,
                f"序号 {order.serial_no} 提交链上登记",
                {"order_id": order_id, "edition_id": order.edition_id, "serial_no": order.serial_no},
            )
            return {"status": "SUBMITTED", "order_id": order_id}

    def confirm_registration(self, order_id: str, receipt_id: str, tx_hash: str) -> dict:
        """链上登记回执：同一回执重放返回原结果，内容变化隔离调查。"""
        with self._lock:
            fingerprint = self._fingerprint({
                "receipt_id": receipt_id, "order_id": order_id, "tx_hash": tx_hash,
            })
            existing = self._receipts.get(receipt_id)
            if existing is not None:
                if existing["fingerprint"] == fingerprint:
                    return {"status": "REPLAYED", "order_id": existing["order_id"],
                            "receipt_id": receipt_id}
                return self._quarantine_receipt(existing, receipt_id, "registration", fingerprint,
                                                {"order_id": order_id, "tx_hash": tx_hash})
            order = self._require_order(order_id)
            self._require_active(order)
            if order.registration not in ("PENDING", "SUBMITTED"):
                raise FulfillmentError("INVALID_STATE", f"登记状态为 {order.registration}")
            self._emit(
                "REGISTRATION_ACCEPTED", "purchase_order", order_id,
                f"链上登记确认（{tx_hash}）",
                {
                    "order_id": order_id,
                    "receipt_id": receipt_id,
                    "fingerprint": fingerprint,
                    "tx_hash": tx_hash,
                    "edition_id": order.edition_id,
                    "serial_no": order.serial_no,
                },
            )
            if order.realname == "APPROVED":
                self._confirm_ownership(order)
            return {"status": "ACCEPTED", "order_id": order_id, "ownership": order.status}

    def fail_registration(self, order_id: str, error: str) -> dict:
        """链上失败：释放尚未完成的资源（序号、实体额度）。"""
        with self._lock:
            order = self._require_order(order_id)
            self._require_active(order)
            if order.registration not in ("PENDING", "SUBMITTED"):
                raise FulfillmentError("INVALID_STATE", f"登记状态为 {order.registration}")
            self._emit(
                "REGISTRATION_FAILED", "purchase_order", order_id,
                f"链上登记失败：{error}",
                {"order_id": order_id, "error": error,
                 "edition_id": order.edition_id, "serial_no": order.serial_no},
            )
            self._release_order(order, "REGISTRATION_FAILED",
                                f"链上登记失败，释放序号 {order.serial_no} 与未履约资源")
            return {"status": "RELEASED", "order_id": order_id}

    # ------------------------------------------------------------------
    # 实体装裱发运
    # ------------------------------------------------------------------

    def request_address_change(self, shipment_id: str, new_address: str,
                               requested_by: str = "buyer") -> dict:
        """地址变更申请：仅出库前可发起，必须经审批才生效。"""
        with self._lock:
            shp = self._require_shipment(shipment_id)
            if shp.status == "ADDRESS_CHANGE_PENDING":
                raise FulfillmentError("INVALID_STATE", "已有待审批的地址变更")
            if shp.status != "CREATED":
                raise FulfillmentError("INVALID_STATE", f"发运状态为 {shp.status}，地址变更不予受理")
            self._emit(
                "ADDRESS_CHANGE_REQUESTED", "shipment", shipment_id,
                f"申请变更收件地址（{requested_by}）",
                {"shipment_id": shipment_id, "new_address": new_address, "requested_by": requested_by,
                 "edition_id": shp.edition_id, "serial_no": shp.serial_no},
            )
            return {"status": "PENDING_APPROVAL", "shipment_id": shipment_id}

    def approve_address_change(self, shipment_id: str, approver: str) -> dict:
        with self._lock:
            shp = self._require_shipment(shipment_id)
            if shp.status != "ADDRESS_CHANGE_PENDING":
                raise FulfillmentError("INVALID_STATE", "没有待审批的地址变更或已出库")
            self._emit(
                "ADDRESS_CHANGE_APPROVED", "shipment", shipment_id,
                f"地址变更审批通过（{approver}）",
                {"shipment_id": shipment_id, "new_address": shp.pending_address, "approver": approver,
                 "edition_id": shp.edition_id, "serial_no": shp.serial_no},
            )
            return {"status": "APPROVED", "shipment_id": shipment_id, "address": shp.address}

    def reject_address_change(self, shipment_id: str, reason: str) -> dict:
        with self._lock:
            shp = self._require_shipment(shipment_id)
            if shp.status != "ADDRESS_CHANGE_PENDING":
                raise FulfillmentError("INVALID_STATE", "没有待审批的地址变更")
            self._emit(
                "ADDRESS_CHANGE_REJECTED", "shipment", shipment_id,
                f"地址变更驳回：{reason}",
                {"shipment_id": shipment_id, "reason": reason,
                 "edition_id": shp.edition_id, "serial_no": shp.serial_no},
            )
            return {"status": "REJECTED", "shipment_id": shipment_id}

    def mark_outbound(self, shipment_id: str) -> dict:
        with self._lock:
            shp = self._require_shipment(shipment_id)
            if shp.status == "ADDRESS_CHANGE_PENDING":
                raise FulfillmentError("INVALID_STATE", "地址变更待审批，禁止出库")
            if shp.status != "CREATED":
                raise FulfillmentError("INVALID_STATE", f"发运状态为 {shp.status}，无法出库")
            self._emit(
                "PHYSICAL_DISPATCHED", "shipment", shipment_id,
                f"序号 {shp.serial_no} 实体装裱出库",
                {"shipment_id": shipment_id, "order_id": shp.order_id,
                 "edition_id": shp.edition_id, "serial_no": shp.serial_no},
            )
            return {"status": "OUTBOUND", "shipment_id": shipment_id}

    def mark_delivered(self, shipment_id: str) -> dict:
        with self._lock:
            shp = self._require_shipment(shipment_id)
            if shp.status != "OUTBOUND":
                raise FulfillmentError("INVALID_STATE", f"发运状态为 {shp.status}，无法签收")
            self._emit(
                "SHIPMENT_DELIVERED", "shipment", shipment_id,
                f"序号 {shp.serial_no} 实体装裱已签收",
                {"shipment_id": shipment_id, "order_id": shp.order_id,
                 "edition_id": shp.edition_id, "serial_no": shp.serial_no},
            )
            return {"status": "DELIVERED", "shipment_id": shipment_id}

    def mark_returned(self, shipment_id: str, reason: str) -> dict:
        """物流退回：仅记录退回事实，资源释放与补救由 process_returns 处理。"""
        with self._lock:
            shp = self._require_shipment(shipment_id)
            if shp.status != "OUTBOUND":
                raise FulfillmentError("INVALID_STATE", f"发运状态为 {shp.status}，无法退回")
            self._emit(
                "SHIPMENT_RETURNED", "shipment", shipment_id,
                f"物流退回：{reason}",
                {"shipment_id": shipment_id, "order_id": shp.order_id, "reason": reason,
                 "edition_id": shp.edition_id, "serial_no": shp.serial_no},
            )
            return {"status": "RETURNED", "shipment_id": shipment_id}

    def process_returns(self) -> list[dict]:
        """退件处理：实体额度回补；数字归属已登记不可回滚，转入补救案件。"""
        with self._lock:
            processed = []
            for shp in list(self._shipments.values()):
                if shp.status != "RETURNED":
                    continue
                self._emit(
                    "SHIPMENT_RESTOCKED", "shipment", shp.shipment_id,
                    f"序号 {shp.serial_no} 实体装裱退回入库",
                    {"shipment_id": shp.shipment_id, "order_id": shp.order_id,
                     "edition_id": shp.edition_id, "serial_no": shp.serial_no},
                )
                order = self._orders[shp.order_id]
                case_id = self._open_case(order, "LOGISTICS_RETURNED")
                processed.append({"shipment_id": shp.shipment_id, "case_id": case_id})
            return processed

    # ------------------------------------------------------------------
    # 补救案件
    # ------------------------------------------------------------------

    def resolve_case(self, case_id: str, resolution: str) -> dict:
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise FulfillmentError("NOT_FOUND", f"补救案件不存在：{case_id}")
            if case.status != "OPEN":
                raise FulfillmentError("INVALID_STATE", f"案件状态为 {case.status}")
            self._emit(
                "ORDER_REMEDIED", "fulfillment_case", case_id,
                f"补救案件办结：{resolution}",
                {"case_id": case_id, "order_id": case.order_id, "resolution": resolution,
                 "edition_id": case.edition_id, "serial_no": case.serial_no},
            )
            return {"status": "RESOLVED", "case_id": case_id}

    # ------------------------------------------------------------------
    # 重启恢复与查询
    # ------------------------------------------------------------------

    def recover(self) -> dict:
        """重启后继续：过期预占释放、待登记订单补登记、退回发运件处理。"""
        with self._lock:
            expired = self.sweep_expired_holds()
            retried = []
            for order in list(self._orders.values()):
                if order.status == "PAID" and order.registration in ("PENDING", "SUBMITTED"):
                    if order.registration == "PENDING":
                        self._emit(
                            "REGISTRATION_SUBMITTED", "purchase_order", order.order_id,
                            f"重启后补登记：序号 {order.serial_no} 重新提交链上登记",
                            {"order_id": order.order_id, "edition_id": order.edition_id,
                             "serial_no": order.serial_no},
                        )
                    retried.append(order.order_id)
            returns = self.process_returns()
            return {"expired_holds": expired, "registrations_retried": retried,
                    "returns_processed": returns}

    def serial_status(self, edition_id: str, serial_no: int) -> str:
        with self._lock:
            edition = self._require_edition(edition_id)
            if not 1 <= serial_no <= edition.total_supply:
                raise FulfillmentError("SERIAL_OUT_OF_RANGE", f"序号越界：{serial_no}")
            if edition.is_reserved(serial_no):
                return "RESERVED"
            order_id = self._serial_order.get((edition_id, serial_no))
            if order_id is not None:
                order = self._orders[order_id]
                return "OWNED" if order.status == "OWNERSHIP_CONFIRMED" else "SOLD"
            res = self._active_reservation(edition_id, serial_no, self._clock.now())
            if res is not None:
                return "HELD"
            return "AVAILABLE"

    def available_serials(self, edition_id: str) -> list[int]:
        with self._lock:
            edition = self._require_edition(edition_id)
            now = self._clock.now()
            return [s for s in range(1, edition.total_supply + 1)
                    if not edition.is_reserved(s)
                    and (edition_id, s) not in self._serial_order
                    and self._active_reservation(edition_id, s, now) is None]

    def physical_committed(self, edition_id: str) -> int:
        with self._lock:
            return self._physical_committed(edition_id)

    def trace_serial(self, edition_id: str, serial_no: int) -> dict:
        """从任一序号还原预占、付款、登记、发运与补救依据。"""
        with self._lock:
            self._require_edition(edition_id)
            evidence = [
                {
                    "event_id": e["event_id"],
                    "event_type": e["event_type"],
                    "aggregate_type": e["aggregate_type"],
                    "aggregate_id": e["aggregate_id"],
                    "occurred_at": e["occurred_at"],
                    "version": e["version"],
                    "summary": e["summary"],
                }
                for e in self._store.events
                if e["payload"].get("edition_id") == edition_id
                and e["payload"].get("serial_no") == serial_no
            ]
            reservations = []
            for res in self._reservations.values():
                if res.edition_id == edition_id and res.serial_no == serial_no:
                    view = asdict(res)
                    view["expires_at"] = res.expires_at.isoformat()
                    reservations.append(view)
            orders = []
            for order in self._orders.values():
                if order.edition_id == edition_id and order.serial_no == serial_no:
                    view = asdict(order)
                    view["shipment_id"] = self._order_shipment.get(order.order_id)
                    view["case_ids"] = list(self._order_cases.get(order.order_id, []))
                    orders.append(view)
            shipments = [asdict(s) for s in self._shipments.values()
                         if s.edition_id == edition_id and s.serial_no == serial_no]
            cases = [asdict(c) for c in self._cases.values()
                     if c.edition_id == edition_id and c.serial_no == serial_no]
            quarantined = [q for q in self._quarantine
                           if q["edition_id"] == edition_id and q["serial_no"] == serial_no]
            return {
                "edition_id": edition_id,
                "serial_no": serial_no,
                "status": self.serial_status(edition_id, serial_no),
                "reservations": reservations,
                "orders": orders,
                "shipments": shipments,
                "remediation_cases": cases,
                "quarantined": quarantined,
                "events": evidence,
            }

    def order_state(self, order_id: str) -> dict:
        with self._lock:
            order = self._require_order(order_id)
            view = asdict(order)
            view["shipment_id"] = self._order_shipment.get(order_id)
            view["case_ids"] = list(self._order_cases.get(order_id, []))
            return view

    def shipment_state(self, shipment_id: str) -> dict:
        with self._lock:
            return asdict(self._require_shipment(shipment_id))

    def case_state(self, case_id: str) -> dict:
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise FulfillmentError("NOT_FOUND", f"补救案件不存在：{case_id}")
            return asdict(case)

    @property
    def events(self) -> list[dict]:
        return self._store.events

    @property
    def quarantined_receipts(self) -> list[dict]:
        return list(self._quarantine)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _require_edition(self, edition_id: str) -> Edition:
        edition = self._editions.get(edition_id)
        if edition is None:
            raise FulfillmentError("NOT_FOUND", f"版次不存在：{edition_id}")
        return edition

    def _require_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise FulfillmentError("NOT_FOUND", f"订单不存在：{order_id}")
        return order

    def _require_shipment(self, shipment_id: str) -> Shipment:
        shp = self._shipments.get(shipment_id)
        if shp is None:
            raise FulfillmentError("NOT_FOUND", f"发运任务不存在：{shipment_id}")
        return shp

    @staticmethod
    def _require_active(order: Order) -> None:
        if order.status == "RELEASED":
            raise FulfillmentError("INVALID_STATE", f"订单 {order.order_id} 已释放")

    def _active_reservation(self, edition_id: str, serial_no: int, now: datetime) -> Reservation | None:
        reservation_id = self._serial_reservation.get((edition_id, serial_no))
        if reservation_id is None:
            return None
        res = self._reservations[reservation_id]
        if res.status == "HELD" and res.expires_at > now:
            return res
        return None

    def _first_available(self, edition: Edition, now: datetime) -> int | None:
        for serial_no in range(1, edition.total_supply + 1):
            if edition.is_reserved(serial_no):
                continue
            if (edition.edition_id, serial_no) in self._serial_order:
                continue
            if self._active_reservation(edition.edition_id, serial_no, now) is not None:
                continue
            return serial_no
        return None

    def _check_serial_available(self, edition: Edition, serial_no: int, now: datetime) -> None:
        if not 1 <= serial_no <= edition.total_supply:
            raise FulfillmentError("SERIAL_OUT_OF_RANGE", f"序号越界：{serial_no}")
        if edition.is_reserved(serial_no):
            raise FulfillmentError("SERIAL_RESERVED", f"序号 {serial_no} 属于保留范围")
        if (edition.edition_id, serial_no) in self._serial_order:
            raise FulfillmentError("SERIAL_UNAVAILABLE", f"序号 {serial_no} 已售出")
        if self._active_reservation(edition.edition_id, serial_no, now) is not None:
            raise FulfillmentError("SERIAL_UNAVAILABLE", f"序号 {serial_no} 已被预占")

    def _physical_committed(self, edition_id: str) -> int:
        committed = sum(
            1 for o in self._orders.values()
            if o.edition_id == edition_id and o.status != "RELEASED" and o.physical_earmarked
        )
        committed += sum(
            1 for s in self._shipments.values()
            if s.edition_id == edition_id and s.status not in ("RESTOCKED", "CANCELLED")
        )
        return committed

    def _confirm_ownership(self, order: Order) -> None:
        self._emit(
            "OWNERSHIP_CONFIRMED", "purchase_order", order.order_id,
            f"序号 {order.serial_no} 数字归属确认",
            {"order_id": order.order_id, "edition_id": order.edition_id,
             "serial_no": order.serial_no},
        )
        if order.physical_earmarked:
            shipment_id = self._next_id("shp")
            self._emit(
                "SHIPMENT_CREATED", "shipment", shipment_id,
                f"序号 {order.serial_no} 实体装裱发运任务",
                {"shipment_id": shipment_id, "order_id": order.order_id,
                 "edition_id": order.edition_id, "serial_no": order.serial_no,
                 "address": order.address},
            )

    def _release_order(self, order: Order, reason: str, summary: str) -> None:
        self._emit(
            "ORDER_RELEASED", "purchase_order", order.order_id,
            summary,
            {"order_id": order.order_id, "reason": reason,
             "edition_id": order.edition_id, "serial_no": order.serial_no},
        )

    def _open_case(self, order: Order, reason: str) -> str:
        case_id = self._next_id("case")
        self._emit(
            "REMEDIATION_OPENED", "fulfillment_case", case_id,
            f"序号 {order.serial_no} 进入补救案件：{reason}",
            {"case_id": case_id, "order_id": order.order_id,
             "edition_id": order.edition_id, "serial_no": order.serial_no, "reason": reason},
        )
        return case_id

    def _quarantine_receipt(self, existing: dict, receipt_id: str, kind: str,
                            received_fingerprint: str, received: dict) -> dict:
        order = self._orders[existing["order_id"]]
        self._emit(
            "RECEIPT_QUARANTINED", "purchase_order", order.order_id,
            f"回执 {receipt_id} 内容变化，隔离调查",
            {"order_id": order.order_id, "receipt_id": receipt_id, "kind": kind,
             "expected_fingerprint": existing["fingerprint"],
             "received_fingerprint": received_fingerprint,
             "received": received,
             "edition_id": order.edition_id, "serial_no": order.serial_no},
        )
        return {"status": "QUARANTINED", "order_id": order.order_id, "receipt_id": receipt_id}
