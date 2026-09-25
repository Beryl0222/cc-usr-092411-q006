"""事件信封与契约 schema 的一致性：履约链产生的每个事件都必须过基础校验。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.validator import validate_event
from tests.fulfillment_helpers import FulfillmentTestCase

SCHEMA = json.loads(
    (Path(__file__).parents[1] / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
)
SCHEMA_EVENT_TYPES = set(SCHEMA["properties"]["event_type"]["enum"])
SCHEMA_AGGREGATES = set(SCHEMA["properties"]["aggregate_type"]["enum"])


class EmittedEventContractTest(FulfillmentTestCase):
    def test_all_emitted_events_match_envelope_and_schema(self):
        _, order_id, _ = self.registered_order()
        self.svc.create_shipment(order_id, "张三", "北京市东城区 1 号", "13800000000")
        self.svc.request_address_change(order_id, "上海市徐汇区 2 号", "客服A")
        self.svc.approve_address_change(order_id, "运营B")
        self.svc.dispatch(order_id, "顺丰", "SF123")
        self.svc.receive_return("ret-1", f"SHIP-{order_id}", "收件人拒收")
        self.svc.recover()

        events = self.store.all_events()
        self.assertGreater(len(events), 10)
        for event in events:
            envelope = event.to_envelope()
            self.assertEqual(validate_event(envelope), [], f"信封校验失败：{event.event_type}")
            self.assertIn(event.event_type, SCHEMA_EVENT_TYPES, f"事件类型未登记入契约：{event.event_type}")
            self.assertIn(event.aggregate_type, SCHEMA_AGGREGATES)

    def test_event_type_constants_all_in_schema(self):
        from src.fulfillment import events as ev

        declared = {
            getattr(ev, name) for name in dir(ev) if name.startswith("EVENT_")
        }
        self.assertEqual(declared, SCHEMA_EVENT_TYPES, "代码事件类型与契约枚举不一致")


if __name__ == "__main__":
    unittest.main()
