"""事件幂等、乱序归段与保管链连续性测试。"""

import tempfile
import unittest

from freight_replanning import FreightService

from helpers import build_network, move_to_b, register_shipment, ts


class EventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.service = FreightService(self.dir.name)
        build_network(self.service)
        registered = register_shipment(self.service)
        self.units = registered["unit_codes"]

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_duplicate_event_is_applied_once(self) -> None:
        first = self.service.ingest_event(event_id="E1", type="load", unit_code=self.units[0],
                                          occurred_at=ts(1, 8), leg_code="L1")
        second = self.service.ingest_event(event_id="E1", type="load", unit_code=self.units[0],
                                           occurred_at=ts(1, 8), leg_code="L1")
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "duplicate")
        custody = self.service.unit_custody(self.units[0])
        self.assertEqual(len(custody["custody"]), 2)  # 节点 → 班次，只推进一次

    def test_out_of_order_events_rebuild_correct_segment(self) -> None:
        unit = self.units[0]
        # 先收到卸货，后收到装货：乱序到达，卸货事件先挂起
        pending = self.service.ingest_event(event_id="E-UNLOAD", type="unload", unit_code=unit,
                                          occurred_at=ts(1, 14), leg_code="L1")
        self.assertEqual(pending["status"], "pending")
        applied = self.service.ingest_event(event_id="E-LOAD", type="load", unit_code=unit,
                                          occurred_at=ts(1, 8), leg_code="L1")
        self.assertEqual(applied["status"], "applied")
        custody = self.service.unit_custody(unit)
        self.assertTrue(custody["custody_continuous"])
        self.assertEqual([r["custodian_code"] for r in custody["custody"]], ["A", "L1", "B"])
        self.assertEqual(custody["location"], {"type": "node", "code": "B"})
        self.assertEqual(custody["state"], "at_hub")
        self.assertFalse(any(e["pending"] for e in custody["events"]))

    def test_temp_deviation_attributed_to_correct_leg(self) -> None:
        move_to_b(self.service, self.units)
        # 温度偏差发生在 L1 在途时段（08:00-14:00 之间）
        self.service.ingest_event(event_id="E-TEMP", type="temp_deviation", unit_code=self.units[0],
                                  occurred_at=ts(1, 10), payload={"temperature": 9.0})
        status = self.service.shipment_status("SH-1")
        self.assertEqual(len(status["liability"]), 1)
        record = status["liability"][0]
        self.assertEqual(record["segment_type"], "leg")
        self.assertEqual(record["segment_code"], "L1")
        self.assertTrue(status["where"][0]["temp_compromised"])

    def test_temp_deviation_at_hub_attributed_to_node(self) -> None:
        move_to_b(self.service, self.units)
        self.service.ingest_event(event_id="E-TEMP2", type="temp_deviation", unit_code=self.units[0],
                                  occurred_at=ts(1, 15), payload={"temperature": 8.5})
        status = self.service.shipment_status("SH-1")
        self.assertEqual(status["liability"][0]["segment_code"], "B")
        self.assertEqual(status["liability"][0]["segment_type"], "node")

    def test_handover_between_legs_keeps_chain_continuous(self) -> None:
        move_to_b(self.service, self.units)
        unit = self.units[0]
        self.service.ingest_event(event_id="E-HANDOVER", type="handover", unit_code=unit,
                                  occurred_at=ts(1, 15), payload={"to_leg": "L7"})
        custody = self.service.unit_custody(unit)
        self.assertTrue(custody["custody_continuous"])
        self.assertEqual([r["custodian_code"] for r in custody["custody"]], ["A", "L1", "B", "L7"])
        self.assertEqual(custody["location"], {"type": "leg", "code": "L7"})

    def test_reject_attributed_to_last_carrier_leg(self) -> None:
        move_to_b(self.service, self.units)
        unit = self.units[0]
        self.service.ingest_event(event_id="E-LOAD-L7", type="load", unit_code=unit,
                                  occurred_at=ts(1, 18), leg_code="L7")
        self.service.ingest_event(event_id="E-UNLOAD-L7", type="unload", unit_code=unit,
                                  occurred_at=ts(1, 22), leg_code="L7")
        self.service.ingest_event(event_id="E-REJECT", type="reject", unit_code=unit,
                                  occurred_at=ts(1, 23), payload={"note": "抽检不合格"})
        status = self.service.shipment_status("SH-1")
        reject = [r for r in status["liability"] if r["event_type"] == "reject"][0]
        self.assertEqual(reject["segment_code"], "L7")

    def test_partial_delivery_not_overwritten_by_unit_failure(self) -> None:
        """一个单元签收、另一个被拒收：整票必须呈现部分交付而非失败。"""
        move_to_b(self.service, self.units)
        self.service.ingest_event(event_id="E-DELIVER", type="deliver", unit_code=self.units[0],
                                  occurred_at=ts(1, 15))
        self.service.ingest_event(event_id="E-REJECT", type="reject", unit_code=self.units[1],
                                  occurred_at=ts(1, 15), payload={"note": "拒收"})
        status = self.service.shipment_status("SH-1")
        self.assertEqual(status["state"], "partially_delivered")
        states = {w["unit_code"]: w["state"] for w in status["where"]}
        self.assertEqual(states[self.units[0]], "delivered")
        self.assertEqual(states[self.units[1]], "rejected")

    def test_conflicting_event_is_parked_as_pending(self) -> None:
        unit = self.units[0]
        # 单元还在 A，不可能装上从 B 出发的 L2：事件挂起但不污染保管链
        result = self.service.ingest_event(event_id="E-BAD", type="load", unit_code=unit,
                                           occurred_at=ts(1, 16), leg_code="L2")
        self.assertEqual(result["status"], "pending")
        custody = self.service.unit_custody(unit)
        self.assertEqual(custody["state"], "planned")
        self.assertEqual([e["event_id"] for e in custody["events"] if e["pending"]], ["E-BAD"])
        # 后续正常事件不受挂起事件影响
        self.service.ingest_event(event_id="E-OK", type="load", unit_code=unit,
                                  occurred_at=ts(1, 8), leg_code="L1")
        custody = self.service.unit_custody(unit)
        self.assertEqual(custody["location"], {"type": "leg", "code": "L1"})
        self.assertEqual([e["event_id"] for e in custody["events"] if e["pending"]], ["E-BAD"])

    def test_all_delivered_fulfills_commitment(self) -> None:
        move_to_b(self.service, self.units)
        for i, unit in enumerate(self.units):
            self.service.ingest_event(event_id=f"E-DEL-{i}", type="deliver", unit_code=unit,
                                      occurred_at=ts(1, 15))
        status = self.service.shipment_status("SH-1")
        self.assertEqual(status["state"], "delivered")
        self.assertTrue(all(c["state"] == "fulfilled" for c in status["commitments"]))


if __name__ == "__main__":
    unittest.main()
