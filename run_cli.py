"""多式联运异常重排命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from freight_replanning import ShipmentPlan


def main() -> None:
    item = ShipmentPlan(plan_code='plan-code-001', shipment_code='shipment-code-001', route_revision='route-revision-001', state='state-001')
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
