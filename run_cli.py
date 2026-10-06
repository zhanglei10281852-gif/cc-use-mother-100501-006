"""多式联运异常重排命令行冒烟入口。

在临时目录中运行完整演示场景（登记班次与货物 → 在途事件 → 容量取消自动改线
→ 部分损坏定责 → 部分签收），并打印货物状态摘要；随后验证基础契约仍可加载。
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from freight_replanning import FreightService, ShipmentPlan
from freight_replanning.cli import _run_demo


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="freight-demo-") as data_dir:
        result = _run_demo(FreightService(data_dir))
        status = result["status"]
        summary = {
            "shipment": status["shipment_code"],
            "state": status["state"],
            "where": status["where"],
            "route_reason": status["route"]["reason"] if status["route"] else None,
            "voided_commitments": [c["commitment_id"] for c in status["voided_commitments"]],
            "liability": status["liability"],
            "fees_net": status["fees"]["net"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    item = ShipmentPlan(plan_code="plan-code-001", shipment_code="shipment-code-001",
                        route_revision="route-revision-001", state="state-001")
    print(json.dumps({"contract_fingerprint": item.fingerprint()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
