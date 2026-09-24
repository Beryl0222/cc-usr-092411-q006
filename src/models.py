"""履约链状态模型。所有实例仅由事件回放或事件应用创建、修改。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Edition:
    edition_id: str
    total_supply: int
    reserved_ranges: list[tuple[int, int]]
    physical_quota: int

    def is_reserved(self, serial_no: int) -> bool:
        return any(start <= serial_no <= end for start, end in self.reserved_ranges)


@dataclass
class Reservation:
    reservation_id: str
    edition_id: str
    serial_no: int
    buyer_id: str
    expires_at: datetime
    status: str = "HELD"  # HELD / CONSUMED / EXPIRED
    order_id: str | None = None


@dataclass
class Order:
    order_id: str
    reservation_id: str
    edition_id: str
    serial_no: int
    buyer_id: str
    payment_id: str
    amount: int
    wants_physical: bool
    address: str | None
    physical_earmarked: bool = False
    status: str = "PAID"  # PAID / OWNERSHIP_CONFIRMED / RELEASED
    realname: str = "PENDING"  # PENDING / APPROVED / REJECTED
    registration: str = "PENDING"  # PENDING / SUBMITTED / ACCEPTED / FAILED
    tx_hash: str | None = None
    registration_receipt_id: str | None = None
    in_remediation: bool = False


@dataclass
class Shipment:
    shipment_id: str
    order_id: str
    edition_id: str
    serial_no: int
    address: str
    # CREATED / ADDRESS_CHANGE_PENDING / OUTBOUND / DELIVERED / RETURNED / RESTOCKED / CANCELLED
    status: str = "CREATED"
    pending_address: str | None = None


@dataclass
class RemediationCase:
    case_id: str
    order_id: str
    edition_id: str
    serial_no: int
    reason: str
    status: str = "OPEN"  # OPEN / RESOLVED
    resolution: str | None = None
