"""候选改线路径搜索与评分。

在"当前节点 → 目的地"的有向班次网络上按时间做优先搜索，
只使用容量充足、温控覆盖、时窗可行且未取消的班次；
封闭枢纽禁止作为到达节点（货物已在封闭枢纽时允许驶离）。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

from .models import Booking, BookingState, LegStatus, RouteChoice, RouteSegment, TransportLeg
from .timeutil import parse

TRANSFER_BUFFER = timedelta(minutes=30)  # 同枢纽换乘最小缓冲
MAX_HOPS = 4


def free_capacity(leg: TransportLeg, bookings: Iterable[Booking]) -> int:
    """班次剩余容量 = 总容量 - 有效占位。"""
    used = sum(
        b.space for b in bookings if b.leg_code == leg.leg_code and b.state == BookingState.RESERVED
    )
    return leg.capacity - used


@dataclass
class _QueueEntry:
    arrive: object  # datetime，堆排序键
    order: int
    node: str
    segments: tuple[RouteSegment, ...]
    fee: float

    def __lt__(self, other: "_QueueEntry") -> bool:
        return (self.arrive, self.order) < (other.arrive, other.order)


def find_candidate_paths(
    *,
    legs: Iterable[TransportLeg],
    bookings: Iterable[Booking],
    start_node: str,
    destination: str,
    not_before: str,
    space_needed: int,
    temp_min: float,
    temp_max: float,
    closed_hubs: Iterable[str] = (),
    max_results: int = 3,
) -> list[RouteChoice]:
    """返回按 (到达时间, 换乘次数, 费用) 排序的可行候选路径。"""
    closed = set(closed_hubs)
    active_bookings = [b for b in bookings if b.state == BookingState.RESERVED]
    by_origin: dict[str, list[TransportLeg]] = {}
    for leg in legs:
        if leg.status == LegStatus.CANCELLED:
            continue
        by_origin.setdefault(leg.origin, []).append(leg)
    for group in by_origin.values():
        group.sort(key=lambda leg: leg.depart_at)

    results: list[RouteChoice] = []
    heap: list[_QueueEntry] = [_QueueEntry(parse(not_before), 0, start_node, (), 0.0)]
    order = 0
    while heap and len(results) < max_results:
        entry = heapq.heappop(heap)
        if entry.node == destination:
            if entry.segments:
                results.append(_to_choice(entry))
            continue
        if len(entry.segments) >= MAX_HOPS:
            continue
        visited = {start_node} | {seg.destination for seg in entry.segments}
        ready = entry.arrive if not entry.segments else entry.arrive + TRANSFER_BUFFER
        for leg in by_origin.get(entry.node, ()):
            if leg.destination in closed:  # 封闭枢纽不可到达/经停
                continue
            if leg.destination in visited:  # 简单路径，避免兜圈
                continue
            if parse(leg.depart_at) < ready:  # 时窗不可行
                continue
            if not leg.covers_temp(temp_min, temp_max):  # 温控不满足
                continue
            if free_capacity(leg, active_bookings) < space_needed:  # 容量不足
                continue
            order += 1
            seg = RouteSegment(leg.leg_code, leg.origin, leg.destination, leg.depart_at, leg.arrive_at)
            heapq.heappush(
                heap,
                _QueueEntry(
                    parse(leg.arrive_at),
                    order,
                    leg.destination,
                    entry.segments + (seg,),
                    round(entry.fee + leg.fee_per_unit * space_needed, 2),
                ),
            )
    results.sort(key=lambda c: (parse(c.eta), c.transfers, c.total_fee))
    return results[:max_results]


def _to_choice(entry: _QueueEntry) -> RouteChoice:
    segments = entry.segments
    eta = segments[-1].arrive_at
    transfers = len(segments) - 1
    hops = " → ".join(
        f"{seg.leg_code}({seg.origin}→{seg.destination} {seg.depart_at[5:16]}起)" for seg in segments
    )
    explanation = (
        f"预计 {eta} 送达，换乘 {transfers} 次，"
        f"合计费用 {entry.fee:.2f}；路径：{hops}"
    )
    return RouteChoice(segments, eta, round(entry.fee, 2), transfers, explanation)
