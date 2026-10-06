"""服务重启后继续处理未交接货物。"""

import tempfile
import unittest

from freight_replanning import FreightService

from helpers import build_network, move_to_b, register_shipment, ts


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_restart_recovers_state_and_continues_processing(self) -> None:
        service = FreightService(self.dir.name)
        build_network(service)
        registered = register_shipment(service)
        units = registered["unit_codes"]
        move_to_b(service, units)
        service.report_disruption(type="hub_closed", target="D")

        # 模拟服务重启：同一数据目录新建实例
        restarted = FreightService(self.dir.name)
        before = restarted.shipment_status("SH-1")
        self.assertEqual([s["leg_code"] for s in before["route"]["segments"]], ["L7", "L8"])
        self.assertEqual(len(before["voided_commitments"]), 1)

        # 恢复扫描：未交接货物、有效预约、承诺一目了然
        recovery = restarted.recover()
        self.assertEqual(len(recovery["unhanded_units"]), 2)
        self.assertEqual(recovery["shipments"]["SH-1"], "in_transit")
        self.assertTrue(recovery["reserved_bookings"])

        # 重启后继续接收事件：沿新路线走货并签收
        for i, unit in enumerate(units):
            restarted.ingest_event(event_id=f"E-LOAD-L7-{i}", type="load", unit_code=unit,
                                   occurred_at=ts(1, 18), leg_code="L7")
            restarted.ingest_event(event_id=f"E-UNLOAD-L7-{i}", type="unload", unit_code=unit,
                                   occurred_at=ts(1, 22), leg_code="L7")
            restarted.ingest_event(event_id=f"E-HANDOVER-{i}", type="handover", unit_code=unit,
                                   occurred_at=ts(1, 23), payload={"to_leg": "L8"})
            restarted.ingest_event(event_id=f"E-DELIVER-{i}", type="deliver", unit_code=unit,
                                   occurred_at=ts(2, 18))
        after = restarted.shipment_status("SH-1")
        self.assertEqual(after["state"], "delivered")
        self.assertTrue(any(c["state"] == "fulfilled" for c in after["commitments"]))
        self.assertFalse(any(c["state"] == "active" for c in after["commitments"]))

        # 再次重启：交付结果仍在
        again = FreightService(self.dir.name)
        self.assertEqual(again.shipment_status("SH-1")["state"], "delivered")
        recovery = again.recover()
        self.assertEqual(recovery["unhanded_units"], [])

    def test_pending_events_retried_after_restart(self) -> None:
        service = FreightService(self.dir.name)
        build_network(service)
        registered = register_shipment(service)
        unit = registered["unit_codes"][0]
        # 乱序：卸货先到（挂起），服务重启后装货到达，recover 自动归段
        service.ingest_event(event_id="E-UNLOAD", type="unload", unit_code=unit,
                             occurred_at=ts(1, 14), leg_code="L1")
        restarted = FreightService(self.dir.name)
        restarted.ingest_event(event_id="E-LOAD", type="load", unit_code=unit,
                               occurred_at=ts(1, 8), leg_code="L1")
        recovery = restarted.recover()
        self.assertEqual(recovery["pending_events"], [])
        custody = restarted.unit_custody(unit)
        self.assertEqual(custody["location"], {"type": "node", "code": "B"})
        self.assertTrue(custody["custody_continuous"])


if __name__ == "__main__":
    unittest.main()
