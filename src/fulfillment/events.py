"""领域事件定义。

事件是不可变的事实记录：一经保存，标识、发生时间与版本不再原地改写，
业务更正只能追加后继事件（与 README 的领域边界约定一致）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ---- 聚合类型（与 contracts/domain.schema.json 对齐） ----
AGG_EDITION = "edition"
AGG_SERIAL_RESERVATION = "serial_reservation"
AGG_PURCHASE_ORDER = "purchase_order"
AGG_FULFILLMENT_CASE = "fulfillment_case"

# ---- 事件类型 ----
EVENT_EDITION_REGISTERED = "EDITION_REGISTERED"
EVENT_SERIAL_HELD = "SERIAL_HELD"
EVENT_SERIAL_HOLD_EXPIRED = "SERIAL_HOLD_EXPIRED"
EVENT_SERIAL_RESERVATION_LINKED = "SERIAL_RESERVATION_LINKED"
EVENT_SERIAL_RESERVATION_RELEASED = "SERIAL_RESERVATION_RELEASED"
EVENT_SERIAL_OWNERSHIP_REGISTERED = "SERIAL_OWNERSHIP_REGISTERED"
EVENT_PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
EVENT_PAYMENT_REVOKED = "PAYMENT_REVOKED"
EVENT_IDENTITY_REVIEW_PASSED = "IDENTITY_REVIEW_PASSED"
EVENT_IDENTITY_REVIEW_REJECTED = "IDENTITY_REVIEW_REJECTED"
EVENT_REGISTRATION_SUBMITTED = "REGISTRATION_SUBMITTED"
EVENT_REGISTRATION_ACCEPTED = "REGISTRATION_ACCEPTED"
EVENT_REGISTRATION_FAILED = "REGISTRATION_FAILED"
EVENT_SHIPMENT_TASK_CREATED = "SHIPMENT_TASK_CREATED"
EVENT_ADDRESS_CHANGE_REQUESTED = "ADDRESS_CHANGE_REQUESTED"
EVENT_ADDRESS_CHANGE_APPROVED = "ADDRESS_CHANGE_APPROVED"
EVENT_ADDRESS_CHANGE_REJECTED = "ADDRESS_CHANGE_REJECTED"
EVENT_PHYSICAL_DISPATCHED = "PHYSICAL_DISPATCHED"
EVENT_SHIPMENT_DELIVERED = "SHIPMENT_DELIVERED"
EVENT_SHIPMENT_RETURNED = "SHIPMENT_RETURNED"
EVENT_ORDER_QUARANTINED = "ORDER_QUARANTINED"
EVENT_ORDER_QUARANTINE_CLEARED = "ORDER_QUARANTINE_CLEARED"
EVENT_ORDER_REMEDIED = "ORDER_REMEDIED"
EVENT_REMEDY_CASE_OPENED = "REMEDY_CASE_OPENED"
EVENT_REMEDY_RESOLVED = "REMEDY_RESOLVED"


@dataclass(frozen=True)
class Event:
    event_id: str | None
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    causation_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "version": self.version,
            "occurred_at": self.occurred_at.isoformat(),
            "causation_id": self.causation_id,
            "summary": self.summary,
            "payload": json.dumps(self.payload, ensure_ascii=False, sort_keys=True),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Event":
        return cls(
            event_id=row["event_id"],
            event_type=row["event_type"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            version=row["version"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            causation_id=row["causation_id"],
            summary=row["summary"],
            payload=json.loads(row["payload"]),
        )

    def to_envelope(self) -> dict[str, Any]:
        """输出与基础信封约定兼容的字典（供校验/外部订阅使用）。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": self.occurred_at.isoformat(),
            "version": self.version,
            "summary": self.summary,
            **self.payload,
        }
