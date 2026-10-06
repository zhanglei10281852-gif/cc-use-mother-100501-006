"""测试共享的网络构造器。

标准网络（均为 2030 年时间，避免与当前时间冲突）：

    A --L1--> B --L2--> D --L5--> E
    A --L3--> C --L4--> D --L6--> E
    B --L7--> C --L8------------> E

L5 只接得上 L2（02:00 到，03:00 发），L4 04:00 到只能接 L6。
L8 是绕开 D 的兜底路径，但到达最晚，评分时总是次选。
"""

from __future__ import annotations

from freight_replanning import FreightService

T = "2030-01-0{}T{}:00:00+00:00"


def ts(day: int, hour: int) -> str:
    return T.format(day, f"{hour:02d}")


def build_network(service: FreightService, *, l2_capacity: int = 4) -> None:
    legs = [
        dict(leg_code="L1", mode="rail", origin="A", destination="B",
             depart_at=ts(1, 8), arrive_at=ts(1, 14), capacity=10,
             temp_min=-5, temp_max=10, fee_per_unit=40),
        dict(leg_code="L2", mode="rail", origin="B", destination="D",
             depart_at=ts(1, 16), arrive_at=ts(2, 2), capacity=l2_capacity,
             temp_min=-5, temp_max=10, fee_per_unit=60),
        dict(leg_code="L3", mode="rail", origin="A", destination="C",
             depart_at=ts(1, 9), arrive_at=ts(1, 15), capacity=10,
             temp_min=-5, temp_max=10, fee_per_unit=35),
        dict(leg_code="L4", mode="rail", origin="C", destination="D",
             depart_at=ts(1, 17), arrive_at=ts(2, 4), capacity=6,
             temp_min=-5, temp_max=10, fee_per_unit=45),
        dict(leg_code="L5", mode="cold_truck", origin="D", destination="E",
             depart_at=ts(2, 3), arrive_at=ts(2, 7), capacity=8,
             temp_min=-2, temp_max=8, fee_per_unit=30),
        dict(leg_code="L6", mode="cold_truck", origin="D", destination="E",
             depart_at=ts(2, 12), arrive_at=ts(2, 16), capacity=8,
             temp_min=-2, temp_max=8, fee_per_unit=30),
        dict(leg_code="L7", mode="rail", origin="B", destination="C",
             depart_at=ts(1, 18), arrive_at=ts(1, 22), capacity=5,
             temp_min=-5, temp_max=10, fee_per_unit=20),
        dict(leg_code="L8", mode="rail", origin="C", destination="E",
             depart_at=ts(2, 5), arrive_at=ts(2, 18), capacity=5,
             temp_min=-5, temp_max=10, fee_per_unit=50),
    ]
    for leg in legs:
        service.register_leg(**leg)


def register_shipment(service: FreightService, code: str = "SH-1", units: int = 2) -> dict:
    return service.register_shipment(
        shipment_code=code, merchant="测试商家", origin="A", destination="E",
        unit_count=units, temp_min=0, temp_max=4, promised_by=ts(3, 0),
    )


def move_to_b(service: FreightService, unit_codes: list[str], prefix: str = "EV") -> None:
    """让单元完成 L1 段并到达枢纽 B。"""
    for i, unit in enumerate(unit_codes):
        service.ingest_event(event_id=f"{prefix}-LOAD-L1-{i}", type="load", unit_code=unit,
                             occurred_at=ts(1, 8), leg_code="L1")
        service.ingest_event(event_id=f"{prefix}-UNLOAD-L1-{i}", type="unload", unit_code=unit,
                             occurred_at=ts(1, 14), leg_code="L1")
