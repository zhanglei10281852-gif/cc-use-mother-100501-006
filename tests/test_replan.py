"""异常重排：枢纽封闭、延误、容量取消、部分损坏、人工选择与并发占位。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from freight_replanning import FreightService

from helpers import build_network, move_to_b, register_shipment, ts


class ReplanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.service = FreightService(self.dir.name)
        build_network(self.service)

    def tearDown(self) -> None:
        self.dir.cleanup()

    # ------------------------------------------------------------------
    def test_hub_closure_replans_around_and_preserves_completed_legs(self) -> None:
        registered = register_shipment(self.service)
        units = registered["unit_codes"]
        move_to_b(self.service, units)  # L1 段已完成
        result = self.service.report_disruption(type="hub_closed", target="D")
        self.assertEqual(result["affected_shipments"], ["SH-1"])
        status = self.service.shipment_status("SH-1")
        route = status["route"]
        # 从当前位置 B 出发绕开封闭的 D：B→C→E
        self.assertEqual([s["leg_code"] for s in route["segments"]], ["L7", "L8"])
        self.assertIn("枢纽 D 封闭", route["reason"])
        # 已完成路段的预约保持 FULFILLED，不被破坏
        state = json.loads(Path(self.dir.name, "state.json").read_text(encoding="utf-8"))
        l1_bookings = [b for b in state["bookings"].values() if b["leg_code"] == "L1"]
        self.assertEqual([b["state"] for b in l1_bookings], ["fulfilled"])
        # 旧承诺失效、新承诺签发
        self.assertEqual(len(status["voided_commitments"]), 1)
        active = [c for c in status["commitments"] if c["state"] == "active"]
        self.assertEqual(len(active), 1)
        # 费用：旧预约退费 + 新预约计费
        self.assertGreater(status["fees"]["refunded"], 0)
        self.assertGreater(status["fees"]["charged"], status["fees"]["refunded"])
        # 重排记录含候选与冻结信息
        replan = status["replans"][0]
        self.assertEqual(replan["state"], "frozen")
        self.assertEqual(replan["chosen_by"], "auto")
        self.assertGreaterEqual(len(replan["candidates"]), 1)

    def test_hub_closure_before_departure_uses_alternative_first_mile(self) -> None:
        register_shipment(self.service)
        self.service.report_disruption(type="hub_closed", target="B")
        status = self.service.shipment_status("SH-1")
        self.assertEqual([s["leg_code"] for s in status["route"]["segments"]], ["L3", "L4", "L6"])

    def test_leg_delay_breaks_connection_and_triggers_replan(self) -> None:
        register_shipment(self.service)
        # L2 延误 10 小时：02:00 才到 D，错过 03:00 的 L5
        result = self.service.report_disruption(
            type="leg_delayed", target="L2", details={"delay_minutes": 600})
        self.assertEqual(result["affected_shipments"], ["SH-1"])
        status = self.service.shipment_status("SH-1")
        self.assertEqual([s["leg_code"] for s in status["route"]["segments"]], ["L3", "L4", "L6"])
        self.assertEqual(len(status["voided_commitments"]), 1)

    def test_leg_delay_with_goods_onboard_only_shifts_commitment(self) -> None:
        registered = register_shipment(self.service)
        units = registered["unit_codes"]
        for i, unit in enumerate(units):
            self.service.ingest_event(event_id=f"E-LOAD-{i}", type="load", unit_code=unit,
                                      occurred_at=ts(1, 8), leg_code="L1")
        before = self.service.shipment_status("SH-1")
        old_eta = before["route"]["eta"]
        result = self.service.report_disruption(
            type="leg_delayed", target="L1", details={"delay_minutes": 120})
        self.assertEqual(result["commitment_adjusted"], ["SH-1"])
        self.assertEqual(result["replans"], [])  # 在途不扯下货物
        after = self.service.shipment_status("SH-1")
        self.assertNotEqual(after["route"]["eta"], old_eta)
        self.assertEqual(len(after["voided_commitments"]), 1)

    def test_capacity_cut_evicts_newest_booking_only(self) -> None:
        register_shipment(self.service, "SH-1")  # 先占位
        register_shipment(self.service, "SH-2")  # 后占位
        # L2 容量 4，两票各占 2；砍掉 2 个容量后只逐出 SH-2
        result = self.service.report_disruption(
            type="capacity_cancelled", target="L2", details={"amount": 2})
        self.assertEqual(result["affected_shipments"], ["SH-2"])
        sh1 = self.service.shipment_status("SH-1")
        sh2 = self.service.shipment_status("SH-2")
        self.assertEqual([s["leg_code"] for s in sh1["route"]["segments"]], ["L1", "L2", "L5"])
        self.assertEqual([s["leg_code"] for s in sh2["route"]["segments"]], ["L3", "L4", "L6"])
        state = json.loads(Path(self.dir.name, "state.json").read_text(encoding="utf-8"))
        l2_reserved = sum(b["space"] for b in state["bookings"].values()
                          if b["leg_code"] == "L2" and b["state"] == "reserved")
        self.assertEqual(l2_reserved, 2)  # 不超售

    def test_full_leg_cancellation_evicts_everyone(self) -> None:
        register_shipment(self.service, "SH-1")
        register_shipment(self.service, "SH-2")
        self.service.report_disruption(
            type="capacity_cancelled", target="L2", details={"cancel_leg": True})
        for code in ("SH-1", "SH-2"):
            status = self.service.shipment_status(code)
            self.assertEqual([s["leg_code"] for s in status["route"]["segments"]],
                             ["L3", "L4", "L6"])

    def test_partial_damage_marks_unit_and_keeps_rest_moving(self) -> None:
        registered = register_shipment(self.service, "SH-1", units=3)
        units = registered["unit_codes"]
        move_to_b(self.service, units)
        self.service.report_disruption(
            type="partial_damage", target="SH-1", occurred_at=ts(1, 15),
            details={"unit_codes": [units[2]], "note": "托盘破损"})
        status = self.service.shipment_status("SH-1")
        states = {w["unit_code"]: w["state"] for w in status["where"]}
        self.assertEqual(states[units[2]], "damaged")
        self.assertEqual(states[units[0]], "at_hub")
        self.assertEqual(status["state"], "in_transit")
        # 损坏发生在枢纽 B：赔付责任落在节点 B
        damage = [r for r in status["liability"] if r["event_type"] == "damage"]
        self.assertEqual(damage[0]["segment_code"], "B")
        # 损坏单元的未来占位被释放并退费
        self.assertGreater(status["fees"]["refunded"], 0)
        # 剩余单元的承诺仍然有效，商家能看到送达时间
        active = [c for c in status["commitments"] if c["state"] == "active"]
        self.assertEqual(len(active), 1)

    def test_no_feasible_candidate_marks_exception_and_recovers_after_capacity_added(self) -> None:
        register_shipment(self.service)
        # 封闭 B、C、D 三个枢纽：彻底无路可走
        for hub in ("B", "C", "D"):
            self.service.report_disruption(type="hub_closed", target=hub)
        status = self.service.shipment_status("SH-1")
        self.assertEqual(status["state"], "exception")
        self.assertIn("无可行改线", status["exception_note"])
        proposed = [r for r in status["replans"] if r["state"] == "proposed"]
        self.assertTrue(proposed)
        replan_id = proposed[-1]["replan_id"]
        # 补充直达运力后刷新重排，自动冻结新方案
        self.service.register_leg(leg_code="RESCUE", mode="truck", origin="A", destination="E",
                                  depart_at=ts(2, 6), arrive_at=ts(2, 20), capacity=5,
                                  temp_min=-5, temp_max=10, fee_per_unit=99)
        refreshed = self.service.refresh_replan(replan_id=replan_id)
        self.assertEqual(refreshed["state"], "frozen")
        status = self.service.shipment_status("SH-1")
        self.assertEqual([s["leg_code"] for s in status["route"]["segments"]], ["RESCUE"])
        self.assertIsNone(status["exception_note"])

    def test_manual_choice_requires_reason_and_is_audited(self) -> None:
        register_shipment(self.service)
        self.service.report_disruption(type="hub_closed", target="B")
        status = self.service.shipment_status("SH-1")
        replan = status["replans"][0]
        with self.assertRaises(ValueError):
            self.service.choose_candidate(replan_id=replan["replan_id"],
                                          candidate_id=replan["candidates"][0]["candidate_id"],
                                          operator="op-1", reason="  ")
        # 人工强制改选另一条候选（即使评分更低）
        other = replan["candidates"][-1]
        chosen = self.service.choose_candidate(
            replan_id=replan["replan_id"], candidate_id=other["candidate_id"],
            operator="op-1", reason="客户要求避开枢纽南")
        self.assertEqual(chosen["chosen_by"], "op-1")
        self.assertEqual(chosen["choose_reason"], "客户要求避开枢纽南")
        self.assertEqual(chosen["chosen_candidate_id"], other["candidate_id"])
        status = self.service.shipment_status("SH-1")
        self.assertIn("人工改线", status["route"]["reason"])

    def test_manual_choice_rejected_after_units_moved(self) -> None:
        registered = register_shipment(self.service)
        self.service.report_disruption(type="hub_closed", target="B")
        replan = self.service.shipment_status("SH-1")["replans"][0]
        # 货物已按新方案发运，离开原重排起点
        move_to_b(self.service, registered["unit_codes"], prefix="EV-MOVED")
        with self.assertRaises(ValueError):
            self.service.choose_candidate(
                replan_id=replan["replan_id"],
                candidate_id=replan["candidates"][-1]["candidate_id"],
                operator="op-1", reason="货物已发出仍强行改线")

    def test_concurrent_replans_never_double_book(self) -> None:
        register_shipment(self.service, "SH-1")
        register_shipment(self.service, "SH-2")
        barrier = threading.Barrier(9)
        errors = []

        def fire(idx: int) -> None:
            barrier.wait()
            try:
                if idx % 2 == 0:
                    self.service.report_disruption(
                        type="capacity_cancelled", target="L2",
                        details={"cancel_leg": True}, disruption_id=f"DS-X-{idx}")
                else:
                    self.service.report_disruption(
                        type="hub_closed", target="D", disruption_id=f"DS-Y-{idx}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(8)]
        barrier_thread = threading.Thread(target=barrier.wait)
        barrier_thread.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        barrier_thread.join()
        self.assertEqual(errors, [])
        state = json.loads(Path(self.dir.name, "state.json").read_text(encoding="utf-8"))
        legs = state["legs"]
        # 任何班次的有效占位不得超过容量
        for leg_code, leg in legs.items():
            reserved = sum(b["space"] for b in state["bookings"].values()
                           if b["leg_code"] == leg_code and b["state"] == "reserved")
            self.assertLessEqual(reserved, leg["capacity"], leg_code)
        # 同一单元不得在时间重叠的两个班次上同时占位
        for unit in state["units"].values():
            bookings = [b for b in state["bookings"].values()
                        if unit["unit_code"] in b["unit_codes"] and b["state"] == "reserved"]
            windows = sorted(
                (legs[b["leg_code"]]["depart_at"], legs[b["leg_code"]]["arrive_at"])
                for b in bookings
            )
            for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
                self.assertLessEqual(e1, s2, f"{unit['unit_code']} 重复占位")
        # 两票货物都有可用路线
        for code in ("SH-1", "SH-2"):
            status = self.service.shipment_status(code)
            self.assertIsNotNone(status["route"])

    def test_same_disruption_reported_twice_is_idempotent(self) -> None:
        register_shipment(self.service)
        first = self.service.report_disruption(type="hub_closed", target="B",
                                               disruption_id="DS-1")
        second = self.service.report_disruption(type="hub_closed", target="B",
                                                disruption_id="DS-1")
        self.assertEqual(second["status"], "duplicate")
        status = self.service.shipment_status("SH-1")
        self.assertEqual(len(status["replans"]), len(first["replans"]))


if __name__ == "__main__":
    unittest.main()
