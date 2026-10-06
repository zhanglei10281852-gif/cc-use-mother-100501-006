"""命令行入口：运营人员查询与处置多式联运异常重排。

用法示例：
    python -m freight_replanning.cli --data-dir ./data demo
    python -m freight_replanning.cli --data-dir ./data status SH-001
    python -m freight_replanning.cli --data-dir ./data serve --port 8080
"""

from __future__ import annotations

import argparse
import json
import sys

from .api import serve
from .serde import to_jsonable
from .service import FreightService
from .timeutil import now_iso, plus_minutes


def _print(payload: object) -> None:
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freight-replanning", description="多式联运异常重排服务")
    parser.add_argument("--data-dir", default="./freight_data", help="状态数据目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="启动 HTTP API 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)

    p = sub.add_parser("add-leg", help="注册运输班次")
    p.add_argument("--leg", required=True)
    p.add_argument("--mode", required=True, choices=["rail", "cold_truck", "truck"])
    p.add_argument("--origin", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--depart", required=True, help="出发时间 ISO8601")
    p.add_argument("--arrive", required=True, help="到达时间 ISO8601")
    p.add_argument("--capacity", type=int, required=True)
    p.add_argument("--temp-min", type=float, required=True)
    p.add_argument("--temp-max", type=float, required=True)
    p.add_argument("--fee", type=float, required=True, help="每包装单元费用")

    p = sub.add_parser("add-shipment", help="登记货物并生成初始方案")
    p.add_argument("--shipment", required=True)
    p.add_argument("--merchant", required=True)
    p.add_argument("--origin", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--units", type=int, required=True)
    p.add_argument("--temp-min", type=float, required=True)
    p.add_argument("--temp-max", type=float, required=True)
    p.add_argument("--promised-by", required=True)

    p = sub.add_parser("event", help="上报运输事件（幂等）")
    p.add_argument("--id", required=True, help="事件幂等键")
    p.add_argument("--type", required=True,
                   choices=["load", "unload", "handover", "deliver", "reject", "temp_deviation", "damage"])
    p.add_argument("--unit", required=True)
    p.add_argument("--at", required=True, help="发生时间 ISO8601")
    p.add_argument("--leg")
    p.add_argument("--node")
    p.add_argument("--payload", default="{}", help="JSON 对象，如 '{\"to_leg\":\"L2\"}'")

    p = sub.add_parser("disrupt", help="上报异常并触发重排")
    p.add_argument("--type", required=True,
                   choices=["hub_closed", "leg_delayed", "capacity_cancelled", "partial_damage"])
    p.add_argument("--target", required=True, help="枢纽/班次/货物编码")
    p.add_argument("--at", help="发生时间 ISO8601")
    p.add_argument("--delay-minutes", type=float, default=0)
    p.add_argument("--amount", type=int, default=0, help="取消的容量")
    p.add_argument("--cancel-leg", action="store_true", help="整班取消")
    p.add_argument("--units", default="", help="部分损坏的单元，逗号分隔")
    p.add_argument("--note", default="")
    p.add_argument("--blocks-current-leg", action="store_true")

    p = sub.add_parser("status", help="查看货物：在哪/为什么/承诺/责任")
    p.add_argument("shipment")

    p = sub.add_parser("unit", help="查看包装单元保管链")
    p.add_argument("unit")

    p = sub.add_parser("replans", help="列出重排记录")
    p.add_argument("--shipment")

    p = sub.add_parser("choose", help="人工强制选择候选方案（必须填写理由）")
    p.add_argument("replan")
    p.add_argument("candidate")
    p.add_argument("--operator", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("refresh", help="重新搜索未冻结重排的候选")
    p.add_argument("replan")

    sub.add_parser("recover", help="重启恢复：扫描未交接货物")
    sub.add_parser("legs", help="列出班次")
    sub.add_parser("shipments", help="列出货物")
    sub.add_parser("demo", help="运行端到端演示场景")
    return parser


def _run_demo(service: FreightService) -> dict:
    """县域生鲜全链路演示：正常在途 → 枢纽封闭改线 → 温度偏差定责 → 部分签收。"""
    now = now_iso()
    t = lambda hours: plus_minutes(now, hours * 60)  # noqa: E731
    legs = [
        dict(leg_code="RAIL-A", mode="rail", origin="县城集货站", destination="枢纽北",
             depart_at=t(2), arrive_at=t(8), capacity=10, temp_min=-5, temp_max=10, fee_per_unit=40),
        dict(leg_code="RAIL-AB", mode="rail", origin="枢纽北", destination="区域仓东",
             depart_at=t(10), arrive_at=t(20), capacity=4, temp_min=-5, temp_max=10, fee_per_unit=60),
        dict(leg_code="RAIL-B", mode="rail", origin="枢纽北", destination="枢纽南",
             depart_at=t(11), arrive_at=t(16), capacity=6, temp_min=-5, temp_max=10, fee_per_unit=35),
        dict(leg_code="RAIL-BC", mode="rail", origin="枢纽南", destination="区域仓东",
             depart_at=t(18), arrive_at=t(26), capacity=6, temp_min=-5, temp_max=10, fee_per_unit=45),
        dict(leg_code="COLD-1", mode="cold_truck", origin="区域仓东", destination="城区门店",
             depart_at=t(21), arrive_at=t(25), capacity=8, temp_min=-2, temp_max=8, fee_per_unit=30),
        dict(leg_code="COLD-2", mode="cold_truck", origin="区域仓东", destination="城区门店",
             depart_at=t(34), arrive_at=t(38), capacity=8, temp_min=-2, temp_max=8, fee_per_unit=30),
    ]
    for leg in legs:
        service.register_leg(**leg)
    registered = service.register_shipment(
        shipment_code="SH-DEMO", merchant="山货优品", origin="县城集货站", destination="城区门店",
        unit_count=3, temp_min=0, temp_max=4, promised_by=t(40),
    )
    units = registered["unit_codes"]
    # 前两段正常在途
    for i, unit in enumerate(units):
        service.ingest_event(event_id=f"EV-LOAD-A-{i}", type="load", unit_code=unit,
                             occurred_at=t(2), leg_code="RAIL-A")
        service.ingest_event(event_id=f"EV-UNLOAD-A-{i}", type="unload", unit_code=unit,
                             occurred_at=t(8), leg_code="RAIL-A")
    # 温度偏差：发生在 RAIL-A 在途期间，责任归 RAIL-A
    service.ingest_event(event_id="EV-TEMP-1", type="temp_deviation", unit_code=units[0],
                         occurred_at=t(5), payload={"temperature": 9.5, "note": "冷机故障 40 分钟"})
    # 枢纽北封闭？不——演示容量取消：RAIL-AB 整班取消，触发自动改线（枢纽北→枢纽南→区域仓东）
    disruption = service.report_disruption(type="capacity_cancelled", target="RAIL-AB",
                                           details={"cancel_leg": True})
    # 一个托盘在枢纽北卸货时发现损坏
    service.report_disruption(type="partial_damage", target="SH-DEMO", occurred_at=t(8.5),
                              details={"unit_codes": [units[2]], "note": "托盘倾倒，外包装破损"})
    # 剩余两件继续走新路线并最终签收一件、拒收一件（整票不被失败覆盖）
    for i, unit in enumerate(units[:2]):
        service.ingest_event(event_id=f"EV-LOAD-B-{i}", type="load", unit_code=unit,
                             occurred_at=t(11), leg_code="RAIL-B")
        service.ingest_event(event_id=f"EV-UNLOAD-B-{i}", type="unload", unit_code=unit,
                             occurred_at=t(16), leg_code="RAIL-B")
        service.ingest_event(event_id=f"EV-HANDOVER-{i}", type="handover", unit_code=unit,
                             occurred_at=t(17), payload={"to_leg": "RAIL-BC"})
        service.ingest_event(event_id=f"EV-UNLOAD-BC-{i}", type="unload", unit_code=unit,
                             occurred_at=t(26), leg_code="RAIL-BC")
        service.ingest_event(event_id=f"EV-LOAD-COLD-{i}", type="load", unit_code=unit,
                             occurred_at=t(34), leg_code="COLD-2")
    service.ingest_event(event_id="EV-DELIVER-0", type="deliver", unit_code=units[0], occurred_at=t(38))
    service.ingest_event(event_id="EV-REJECT-1", type="reject", unit_code=units[1],
                         occurred_at=t(38), payload={"note": "门店抽检温度超标拒收"})
    return {
        "disruption": disruption,
        "status": service.shipment_status("SH-DEMO"),
        "unit_custody": service.unit_custody(units[0]),
        "recover": service.recover(),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = FreightService(args.data_dir)
    try:
        if args.command == "serve":
            serve(service, args.host, args.port)
            return 0
        if args.command == "add-leg":
            _print(service.register_leg(
                leg_code=args.leg, mode=args.mode, origin=args.origin, destination=args.destination,
                depart_at=args.depart, arrive_at=args.arrive, capacity=args.capacity,
                temp_min=args.temp_min, temp_max=args.temp_max, fee_per_unit=args.fee,
            ))
        elif args.command == "add-shipment":
            _print(service.register_shipment(
                shipment_code=args.shipment, merchant=args.merchant, origin=args.origin,
                destination=args.destination, unit_count=args.units,
                temp_min=args.temp_min, temp_max=args.temp_max, promised_by=args.promised_by,
            ))
        elif args.command == "event":
            _print(service.ingest_event(
                event_id=args.id, type=args.type, unit_code=args.unit, occurred_at=args.at,
                leg_code=args.leg, node=args.node, payload=json.loads(args.payload),
            ))
        elif args.command == "disrupt":
            details: dict = {"note": args.note}
            if args.type == "leg_delayed":
                details["delay_minutes"] = args.delay_minutes
            if args.type == "capacity_cancelled":
                details["amount"] = args.amount
                details["cancel_leg"] = args.cancel_leg
            if args.type == "partial_damage":
                details["unit_codes"] = [u for u in args.units.split(",") if u]
                details["blocks_current_leg"] = args.blocks_current_leg
            _print(service.report_disruption(type=args.type, target=args.target,
                                             occurred_at=args.at, details=details))
        elif args.command == "status":
            _print(service.shipment_status(args.shipment))
        elif args.command == "unit":
            _print(service.unit_custody(args.unit))
        elif args.command == "replans":
            _print(service.list_replans(args.shipment))
        elif args.command == "choose":
            _print(service.choose_candidate(replan_id=args.replan, candidate_id=args.candidate,
                                            operator=args.operator, reason=args.reason))
        elif args.command == "refresh":
            _print(service.refresh_replan(replan_id=args.replan))
        elif args.command == "recover":
            _print(service.recover())
        elif args.command == "legs":
            _print(service.list_legs())
        elif args.command == "shipments":
            _print(service.list_shipments())
        elif args.command == "demo":
            _print(_run_demo(service))
        return 0
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
