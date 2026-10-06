"""命令行入口测试。"""

import contextlib
import io
import json
import tempfile
import unittest

from freight_replanning.cli import main

from helpers import ts


def run_cli(*args: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.base = ["--data-dir", self.dir.name]

    def tearDown(self) -> None:
        self.dir.cleanup()

    def _ok(self, *args: str) -> dict:
        code, out, err = run_cli(*self.base, *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_full_cli_flow(self) -> None:
        self._ok("add-leg", "--leg", "R1", "--mode", "rail", "--origin", "A",
                 "--destination", "B", "--depart", ts(1, 8), "--arrive", ts(1, 14),
                 "--capacity", "5", "--temp-min", "-5", "--temp-max", "10", "--fee", "25")
        self._ok("add-leg", "--leg", "R2", "--mode", "cold_truck", "--origin", "B",
                 "--destination", "C", "--depart", ts(1, 15), "--arrive", ts(1, 22),
                 "--capacity", "5", "--temp-min", "-2", "--temp-max", "8", "--fee", "35")
        created = self._ok("add-shipment", "--shipment", "SH-CLI", "--merchant", "商户乙",
                           "--origin", "A", "--destination", "C", "--units", "2",
                           "--temp-min", "0", "--temp-max", "4", "--promised-by", ts(3, 0))
        unit = created["unit_codes"][0]

        event = self._ok("event", "--id", "EV-CLI-1", "--type", "load", "--unit", unit,
                         "--at", ts(1, 8), "--leg", "R1")
        self.assertEqual(event["status"], "applied")

        disruption = self._ok("disrupt", "--type", "leg_delayed", "--target", "R2",
                              "--delay-minutes", "600")
        self.assertEqual(disruption["affected_shipments"], ["SH-CLI"])

        status = self._ok("status", "SH-CLI")
        self.assertIn("route", status)
        self.assertIn("commitments", status)
        self.assertIn("liability", status)

        custody = self._ok("unit", unit)
        self.assertTrue(custody["custody_continuous"])

        replans = self._ok("replans", "--shipment", "SH-CLI")
        self.assertEqual(len(replans), 1)

        recovery = self._ok("recover")
        self.assertEqual(len(recovery["unhanded_units"]), 2)

        legs = self._ok("legs")
        self.assertEqual(len(legs), 2)
        shipments = self._ok("shipments")
        self.assertEqual(shipments[0]["shipment_code"], "SH-CLI")

    def test_choose_without_reason_fails(self) -> None:
        self._ok("add-leg", "--leg", "R1", "--mode", "rail", "--origin", "A",
                 "--destination", "B", "--depart", ts(1, 8), "--arrive", ts(1, 14),
                 "--capacity", "5", "--temp-min", "-5", "--temp-max", "10", "--fee", "25")
        code, _, err = run_cli(*self.base, "choose", "RP-0001", "RP-0001-C1",
                               "--operator", "op", "--reason", " ")
        self.assertEqual(code, 1)
        self.assertIn("错误", err)

    def test_demo_scenario_runs_end_to_end(self) -> None:
        result = self._ok("demo")
        status = result["status"]
        self.assertEqual(status["state"], "partially_delivered")
        self.assertTrue(status["voided_commitments"])
        self.assertTrue(status["liability"])
        self.assertIn("自动改线", status["route"]["reason"])
        self.assertTrue(result["unit_custody"]["custody_continuous"])


if __name__ == "__main__":
    unittest.main()
