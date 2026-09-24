"""领域事件类型与聚合类型常量。

与 contracts/domain.schema.json 的枚举保持一致；新增事件类型时两处同步更新，
validator 会对未知类型报错，防止约定漂移。
"""

AGGREGATE_TYPES = (
    "edition",
    "serial_reservation",
    "purchase_order",
    "shipment",
    "fulfillment_case",
)

EVENT_TYPES = (
    # 版次
    "EDITION_REGISTERED",
    # 序号预占
    "SERIAL_HELD",
    "SERIAL_HOLD_EXPIRED",
    # 付款与实名复核
    "PAYMENT_CONFIRMED",
    "PAYMENT_REVOKED",
    "REALNAME_APPROVED",
    "REALNAME_REJECTED",
    # 链上登记与归属
    "REGISTRATION_SUBMITTED",
    "REGISTRATION_ACCEPTED",
    "REGISTRATION_FAILED",
    "OWNERSHIP_CONFIRMED",
    "ORDER_RELEASED",
    "RECEIPT_QUARANTINED",
    # 实体装裱发运
    "SHIPMENT_CREATED",
    "ADDRESS_CHANGE_REQUESTED",
    "ADDRESS_CHANGE_APPROVED",
    "ADDRESS_CHANGE_REJECTED",
    "PHYSICAL_DISPATCHED",
    "SHIPMENT_DELIVERED",
    "SHIPMENT_RETURNED",
    "SHIPMENT_RESTOCKED",
    "SHIPMENT_CANCELLED",
    # 补救案件
    "REMEDIATION_OPENED",
    "ORDER_REMEDIED",
)
