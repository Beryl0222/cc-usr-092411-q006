"""链上登记网关端口与测试替身。

网关只负责把登记请求提交上链并返回回执；领域层不感知链的实现。
回执内容（registration_ref 对应的成败与交易哈希）由应用层做幂等登记，
同一回执完全重放返回原结果，内容变化按冲突拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ChainReceipt:
    registration_ref: str
    accepted: bool
    chain_tx_hash: str | None = None
    reason: str | None = None
    retriable: bool = False


class ChainGateway(Protocol):
    def submit(self, registration_ref: str, order_id: str, edition_id: str, serial: int) -> ChainReceipt:
        """提交登记并同步返回回执；实现方负责自身超时与重试策略。"""


class StubChainGateway:
    """测试/联调用网关：按 registration_ref 预置回执或故障，缺省视为受理成功。"""

    def __init__(self) -> None:
        self._scripted: dict[str, ChainReceipt] = {}
        self._errors: dict[str, Exception] = {}
        self.submissions: list[str] = []

    def script(self, receipt: ChainReceipt) -> None:
        self._scripted[receipt.registration_ref] = receipt

    def script_error(self, registration_ref: str, exc: Exception) -> None:
        """模拟网关故障：提交该登记时抛错，回执留待恢复扫描。"""
        self._errors[registration_ref] = exc

    def clear_error(self, registration_ref: str) -> None:
        self._errors.pop(registration_ref, None)

    def submit(self, registration_ref: str, order_id: str, edition_id: str, serial: int) -> ChainReceipt:
        self.submissions.append(registration_ref)
        if registration_ref in self._errors:
            raise self._errors[registration_ref]
        if registration_ref in self._scripted:
            return self._scripted[registration_ref]
        return ChainReceipt(
            registration_ref=registration_ref,
            accepted=True,
            chain_tx_hash=f"0xstub-{registration_ref}",
        )
