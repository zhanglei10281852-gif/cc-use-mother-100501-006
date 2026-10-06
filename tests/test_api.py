"""HTTP API 端到端测试。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from freight_replanning import FreightService
from freight_replanning.api import create_server

from helpers import ts


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = tempfile.TemporaryDirectory()
        cls.service = FreightService(cls.dir.name)
        cls.server = create_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        code, health = self._call("GET", "/health")
        self.assertEqual((code, health["status"]), (200, "ok"))

        legs = [
            ("R1", "A", "B", ts(1, 8), ts(1, 14)),
            ("R2", "B", "C", ts(1, 15), ts(1, 22)),
        ]
        for leg_code, origin, dest, depart, arrive in legs:
            status, _ = self._call("POST", "/legs", {
                "leg_code": leg_code, "mode": "rail", "origin": origin, "destination": dest,
                "depart_at": depart, "arrive_at": arrive, "capacity": 5,
                "temp_min": -5, "temp_max": 10, "fee_per_unit": 25,
            })
            self.assertEqual(status, 201)

        status, created = self._call("POST", "/shipments", {
            "shipment_code": "SH-API", "merchant": "商户甲", "origin": "A", "destination": "C",
            "unit_count": 1, "temp_min": 0, "temp_max": 4, "promised_by": ts(3, 0),
        })
        self.assertEqual(status, 201)
        unit = created["unit_codes"][0]

        status, event = self._call("POST", "/events", {
            "event_id": "EV-API-1", "type": "load", "unit_code": unit,
            "occurred_at": ts(1, 8), "leg_code": "R1",
        })
        self.assertEqual(status, 200)
        self.assertEqual(event["status"], "applied")
        # 重复上报同一事件：幂等
        status, event = self._call("POST", "/events", {
            "event_id": "EV-API-1", "type": "load", "unit_code": unit,
            "occurred_at": ts(1, 8), "leg_code": "R1",
        })
        self.assertEqual(event["status"], "duplicate")

        # 在途时续程班次被取消：货物到达枢纽后自动触发重排
        status, disruption = self._call("POST", "/disruptions", {
            "type": "capacity_cancelled", "target": "R2", "details": {"cancel_leg": True},
        })
        self.assertEqual(status, 200)
        status, event = self._call("POST", "/events", {
            "event_id": "EV-API-2", "type": "unload", "unit_code": unit,
            "occurred_at": ts(1, 14), "leg_code": "R1",
        })
        self.assertEqual(event["status"], "applied")

        status, shipment = self._call("GET", "/shipments/SH-API")
        self.assertEqual(status, 200)
        self.assertEqual(shipment["state"], "exception")  # 无替代路径，等待处置
        self.assertEqual(shipment["where"][0]["location_code"], "B")

        status, custody = self._call("GET", f"/units/{unit}")
        self.assertEqual(status, 200)
        self.assertTrue(custody["custody_continuous"])

        status, replans = self._call("GET", "/replans?shipment=SH-API")
        self.assertEqual(status, 200)
        self.assertEqual(len(replans), 1)
        replan_id = replans[0]["replan_id"]

        # 人工选择必须给出理由
        status, error = self._call("POST", f"/replans/{replan_id}/choose", {
            "candidate_id": "X", "operator": "op", "reason": "",
        })
        self.assertEqual(status, 400)

        status, recovery = self._call("POST", "/recover")
        self.assertEqual(status, 200)
        self.assertEqual(len(recovery["unhanded_units"]), 1)

        status, _ = self._call("GET", "/shipments/NOPE")
        self.assertEqual(status, 404)

    def test_unknown_route_returns_404(self) -> None:
        status, _ = self._call("GET", "/nope")
        self.assertEqual(status, 404)

    def test_bad_request_returns_400(self) -> None:
        status, error = self._call("POST", "/legs", {"leg_code": "X"})
        self.assertEqual(status, 400)
        self.assertIn("error", error)
        status, _ = self._call("POST", "/events", {"event_id": "E", "type": "load",
                                                   "unit_code": "NOPE", "occurred_at": ts(1, 8)})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
