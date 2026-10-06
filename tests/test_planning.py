"""候选路径搜索的约束与评分测试。"""

import unittest

from freight_replanning.models import Booking, BookingState, TransportLeg, LegMode
from freight_replanning.planning import find_candidate_paths, free_capacity

from helpers import ts


def leg(code: str, origin: str, dest: str, depart: str, arrive: str,
        capacity: int = 5, temp=(-5.0, 10.0), fee: float = 10.0) -> TransportLeg:
    return TransportLeg(leg_code=code, mode=LegMode.RAIL, origin=origin, destination=dest,
                        depart_at=depart, arrive_at=arrive, capacity=capacity,
                        temp_min=temp[0], temp_max=temp[1], fee_per_unit=fee)


def booking(leg_code: str, space: int, bid: str = "BK-1") -> Booking:
    return Booking(booking_id=bid, shipment_code="SH-X", leg_code=leg_code,
                   unit_codes=tuple(f"U{i}" for i in range(space)), space=space, fee=0,
                   state=BookingState.RESERVED, created_at=ts(1, 0))


class PlanningTests(unittest.TestCase):
    def test_capacity_constraint_excludes_full_leg(self) -> None:
        legs = [leg("L1", "A", "B", ts(1, 8), ts(1, 12), capacity=2)]
        choices = find_candidate_paths(
            legs=legs, bookings=[booking("L1", 2)], start_node="A", destination="B",
            not_before=ts(1, 0), space_needed=1, temp_min=0, temp_max=4,
        )
        self.assertEqual(choices, [])
        choices = find_candidate_paths(
            legs=legs, bookings=[booking("L1", 1)], start_node="A", destination="B",
            not_before=ts(1, 0), space_needed=1, temp_min=0, temp_max=4,
        )
        self.assertEqual(len(choices), 1)

    def test_temp_capability_must_cover_requirement(self) -> None:
        legs = [
            leg("WARM", "A", "B", ts(1, 8), ts(1, 12), temp=(2.0, 10.0)),  # 最低 2℃，不覆盖 0℃
            leg("COLD", "A", "B", ts(1, 9), ts(1, 13), temp=(-5.0, 8.0)),
        ]
        choices = find_candidate_paths(
            legs=legs, bookings=[], start_node="A", destination="B",
            not_before=ts(1, 0), space_needed=1, temp_min=0, temp_max=4,
        )
        self.assertEqual([c.segments[0].leg_code for c in choices], ["COLD"])

    def test_transfer_buffer_enforced(self) -> None:
        legs = [
            leg("L1", "A", "B", ts(1, 8), ts(1, 12)),
            leg("L2", "B", "C", ts(1, 12), ts(1, 18)),  # 到达即发车，无换乘缓冲
            leg("L3", "B", "C", ts(1, 13), ts(1, 19)),
        ]
        choices = find_candidate_paths(
            legs=legs, bookings=[], start_node="A", destination="C",
            not_before=ts(1, 0), space_needed=1, temp_min=0, temp_max=4,
        )
        self.assertEqual(len(choices), 1)
        self.assertEqual([s.leg_code for s in choices[0].segments], ["L1", "L3"])

    def test_closed_hub_is_avoided(self) -> None:
        legs = [
            leg("L1", "A", "B", ts(1, 8), ts(1, 12)),
            leg("L2", "B", "D", ts(1, 13), ts(1, 18)),
            leg("L3", "A", "C", ts(1, 9), ts(1, 14)),
            leg("L4", "C", "D", ts(1, 15), ts(1, 20)),
        ]
        choices = find_candidate_paths(
            legs=legs, bookings=[], start_node="A", destination="D",
            not_before=ts(1, 0), space_needed=1, temp_min=0, temp_max=4,
            closed_hubs={"B"},
        )
        self.assertEqual(len(choices), 1)
        self.assertEqual([s.leg_code for s in choices[0].segments], ["L3", "L4"])

    def test_scoring_prefers_earliest_arrival_then_transfers(self) -> None:
        legs = [
            leg("SLOW", "A", "D", ts(1, 8), ts(1, 20)),
            leg("F1", "A", "B", ts(1, 8), ts(1, 12)),
            leg("F2", "B", "D", ts(1, 13), ts(1, 15)),
            leg("DIRECT", "A", "D", ts(1, 9), ts(1, 15)),
        ]
        choices = find_candidate_paths(
            legs=legs, bookings=[], start_node="A", destination="D",
            not_before=ts(1, 0), space_needed=1, temp_min=0, temp_max=4,
        )
        # 直达与换乘同为 15:00 到：直达换乘少，排第一
        self.assertEqual([s.leg_code for s in choices[0].segments], ["DIRECT"])
        self.assertEqual(choices[0].transfers, 0)
        self.assertEqual(choices[-1].segments[-1].leg_code, "SLOW")

    def test_free_capacity_counts_only_reserved(self) -> None:
        target = leg("L1", "A", "B", ts(1, 8), ts(1, 12), capacity=3)
        bookings = [
            booking("L1", 2, "BK-1"),
            Booking(booking_id="BK-2", shipment_code="SH-X", leg_code="L1", unit_codes=("U9",),
                    space=1, fee=0, state=BookingState.RELEASED, created_at=ts(1, 0)),
        ]
        self.assertEqual(free_capacity(target, bookings), 1)


if __name__ == "__main__":
    unittest.main()
