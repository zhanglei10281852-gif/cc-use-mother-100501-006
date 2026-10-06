"""多式联运网络与路径搜索。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Hub, LegSchedule, TempClass


@dataclass(frozen=True, slots=True)
class PathOption:
    """一条可行的班次组合路径。"""

    legs: tuple[LegSchedule, ...]

    @property
    def depart_at(self) -> datetime:
        return self.legs[0].depart_at

    @property
    def arrive_at(self) -> datetime:
        return self.legs[-1].arrive_at

    @property
    def transfers(self) -> int:
        return len(self.legs) - 1

    @property
    def leg_codes(self) -> tuple[str, ...]:
        return tuple(leg.leg_code for leg in self.legs)


class TransportNetwork:
    """由枢纽与运输班次组成的多式联运网络。"""

    def __init__(
        self,
        hubs: dict[str, Hub],
        legs: dict[str, LegSchedule],
    ) -> None:
        self._hubs = hubs
        self._legs = legs

    @property
    def hubs(self) -> dict[str, Hub]:
        return self._hubs

    @property
    def legs(self) -> dict[str, LegSchedule]:
        return self._legs

    def transfer_buffer(self, hub_code: str) -> timedelta:
        hub = self._hubs.get(hub_code)
        minutes = hub.transfer_buffer_minutes if hub else 30
        return timedelta(minutes=minutes)

    def find_paths(
        self,
        origin: str,
        destination: str,
        earliest_depart: datetime,
        temp_requirement: TempClass,
        *,
        max_transfers: int = 4,
        excluded_legs: frozenset[str] = frozenset(),
    ) -> list[PathOption]:
        """搜索满足时窗衔接、温控与枢纽开放状态的班次组合。

        结果按到达时间、换乘次数升序排列。
        """
        if origin == destination:
            return []
        results: list[PathOption] = []
        legs = sorted(self._legs.values(), key=lambda l: (l.depart_at, l.leg_code))

        def usable(leg: LegSchedule) -> bool:
            if leg.leg_code in excluded_legs:
                return False
            if leg.status.value == "cancelled":
                return False
            if not leg.supports(temp_requirement):
                return False
            for code in (leg.origin, leg.destination):
                hub = self._hubs.get(code)
                if hub is not None and not hub.is_open:
                    return False
            return True

        def walk(
            current: str,
            ready_at: datetime,
            chosen: list[LegSchedule],
            visited: frozenset[str],
        ) -> None:
            if len(chosen) > max_transfers + 1:
                return
            for leg in legs:
                if leg.origin != current or not usable(leg):
                    continue
                if leg.depart_at < ready_at:
                    continue
                if leg.destination in visited:
                    continue
                chosen.append(leg)
                if leg.destination == destination:
                    results.append(PathOption(tuple(chosen)))
                else:
                    next_ready = leg.arrive_at + self.transfer_buffer(leg.destination)
                    walk(leg.destination, next_ready, chosen, visited | {leg.destination})
                chosen.pop()

        walk(origin, earliest_depart, [], frozenset({origin}))
        results.sort(key=lambda p: (p.arrive_at, p.transfers, p.leg_codes))
        return results
