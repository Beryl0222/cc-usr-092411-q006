"""履约链统一错误类型。"""


class FulfillmentError(Exception):
    """所有业务拒绝的基类。"""


class StaleState(FulfillmentError):
    """聚合版本过期（乐观锁冲突）。"""


class SerialUnavailable(FulfillmentError):
    """序号不存在、处于保留范围、已被占用或预占已失效。"""


class HoldExpired(FulfillmentError):
    """预占已过期，必须先释放或重新预占。"""


class IllegalTransition(FulfillmentError):
    """当前状态不允许该操作。"""


class ReceiptMismatch(FulfillmentError):
    """同一回执携带了与首次请求不一致的关键内容。"""


class ShipmentNotOutbound(FulfillmentError):
    """实体已出库，地址不再允许变更。"""


class AddressChangePending(FulfillmentError):
    """已有一条待审批的地址变更。"""


class RegistrationIrreversible(FulfillmentError):
    """数字归属已登记，不能回滚，只能进入补救案件。"""


class CaseError(FulfillmentError):
    """补救案件状态不允许该操作。"""
