"""数字草书限量统一履约链。

版次登记总量与保留范围 → 序号预占（可注入时钟过期）→ 付款锁定购买资格 →
实名复核 + 链上登记确认归属 → 实体发运（出库前地址变更需追加审批）→
支付撤销/链上失败/物流退回分别释放未完成资源；已登记数字归属不可回滚，只进补救案件。
"""

from .clock import Clock, FixedClock, SystemClock
from .errors import (
    AddressChangePending,
    CaseError,
    FulfillmentError,
    HoldExpired,
    IllegalTransition,
    ReceiptMismatch,
    RegistrationIrreversible,
    SerialUnavailable,
    ShipmentNotOutbound,
)
from .gateway import ChainGateway, ChainReceipt, StubChainGateway
from .service import DEFAULT_HOLD_TTL, FulfillmentService
from .store import EventStore

__all__ = [
    "AddressChangePending",
    "CaseError",
    "ChainGateway",
    "ChainReceipt",
    "Clock",
    "DEFAULT_HOLD_TTL",
    "EventStore",
    "FixedClock",
    "FulfillmentError",
    "FulfillmentService",
    "HoldExpired",
    "IllegalTransition",
    "ReceiptMismatch",
    "RegistrationIrreversible",
    "SerialUnavailable",
    "ShipmentNotOutbound",
    "StubChainGateway",
    "SystemClock",
]
