import json
import unittest
from pathlib import Path

from src.events import EVENT_TYPES
from src.validator import validate_event

from support import HOLD_TTL, FulfillmentTestCase


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])


class EmittedEventsContractTest(FulfillmentTestCase):
    """履约链发出的每一种事件都必须符合信封约定。"""

    def test_all_emitted_event_types_match_envelope(self) -> None:
        svc = self.service
        svc.register_edition("ed-1", total_supply=8, physical_quota=2)
        # 过期释放（重启后由 recover 清扫）
        svc.hold_serial("ed-1", "buyer-expire")
        # 支付撤销（归属前释放）
        _, pay_revoked = self.paid_order("buyer-b", "pay-revoked")
        svc.revoke_payment(pay_revoked["order_id"], "买家退款")
        # 实名驳回
        _, pay_rejected = self.paid_order("buyer-c", "pay-rejected")
        svc.reject_realname(pay_rejected["order_id"], "材料不符")
        # 链上失败
        _, pay_failed = self.paid_order("buyer-d", "pay-failed")
        svc.submit_registration(pay_failed["order_id"])
        svc.fail_registration(pay_failed["order_id"], "链上错误")
        # 回执内容变化 → 隔离调查
        hold_q, _ = self.paid_order("buyer-e", "pay-quarantine")
        svc.confirm_payment(hold_q["reservation_id"], "pay-quarantine", 1)
        # 归属 + 发运 + 地址变更审批 + 出库 + 签收
        hold_a, _ = self.owned_order("buyer-f", "pay-owned",
                                     wants_physical=True, address="地址甲")
        shipment_id = svc.trace_serial("ed-1", hold_a["serial_no"])["shipments"][0]["shipment_id"]
        svc.request_address_change(shipment_id, "地址乙")
        svc.reject_address_change(shipment_id, "保持原地址")
        svc.request_address_change(shipment_id, "地址丙")
        svc.approve_address_change(shipment_id, approver="ops-2")
        svc.mark_outbound(shipment_id)
        svc.mark_delivered(shipment_id)
        # 归属后支付撤销 → 未出库发运取消 + 补救案件办结
        _, pay_remediated = self.owned_order("buyer-g", "pay-remediated",
                                             wants_physical=True, address="地址庚")
        result = svc.revoke_payment(pay_remediated["order_id"], "渠道撤单")
        svc.resolve_case(result["case_id"], "线下协商完成")
        # 物流退回 + 重启后退件处理
        hold_r, _ = self.owned_order("buyer-h", "pay-returned",
                                     wants_physical=True, address="地址辛")
        returned_id = svc.trace_serial("ed-1", hold_r["serial_no"])["shipments"][0]["shipment_id"]
        svc.mark_outbound(returned_id)
        svc.mark_returned(returned_id, "拒收")
        # 重启恢复：过期释放、补登记、退件处理
        self.clock.advance(HOLD_TTL)
        self.restart()
        svc = self.service
        svc.recover()

        emitted = {e["event_type"] for e in svc.events}
        self.assertEqual(emitted, set(EVENT_TYPES))
        for event in svc.events:
            self.assertEqual(validate_event(event), [], event["event_id"])


if __name__ == "__main__":
    unittest.main()
