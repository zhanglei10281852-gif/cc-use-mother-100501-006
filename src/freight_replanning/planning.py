"""候选改线生成、评估与比较。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Mapping

from .models import (
    Candidate,
    CandidatePart,
    LegSchedule,
    PlanSegment,
    SegmentState,
    TempClass,
    stable_code,
)
from .network import PathOption, TransportNetwork

# 评分权重：逾期时间主导，其次费用，再次换乘次数。
LATE_WEIGHT_PER_HOUR = 100.0
FEE_WEIGHT_PER_YUAN = 1.0
TRANSFER_WEIGHT = 10.0


def available_units(leg: LegSchedule, reserved: Mapping[str, int]) -> int:
    return leg.capacity_total - reserved.get(leg.leg_code, 0)


def _score(eta: datetime, promise: datetime, fee_cents: int, transfers: int) -> float:
    late_hours = max(0.0, (eta - promise).total_seconds() / 3600.0)
    return (
        late_hours * LATE_WEIGHT_PER_HOUR
        + fee_cents / 100.0 * FEE_WEIGHT_PER_YUAN
        + transfers * TRANSFER_WEIGHT
    )


def evaluate_path(
    path: PathOption,
    unit_codes: tuple[str, ...],
    promise: datetime,
    reserved: Mapping[str, int],
    *,
    capacity_override: Mapping[str, int] | None = None,
) -> Candidate:
    """把一条路径评估为单组候选；容量不足时给出不可行原因。"""
    need = len(unit_codes)
    reasons: list[str] = []
    fee = 0
    for leg in path.legs:
        capacity = (
            capacity_override.get(leg.leg_code, leg.capacity_total)
            if capacity_override
            else leg.capacity_total
        )
        left = capacity - reserved.get(leg.leg_code, 0)
        if left < need:
            reasons.append(
                f"班次 {leg.leg_code} 剩余容量 {left} 不足，需要 {need}"
            )
        fee += leg.fee_for(need)
    eta = path.arrive_at
    score = _score(eta, promise, fee, path.transfers)
    code = stable_code(
        "CAND", path.leg_codes, sorted(unit_codes), path.depart_at.isoformat()
    )
    return Candidate(
        candidate_code=code,
        parts=(
            CandidatePart(
                unit_codes=unit_codes,
                leg_codes=path.leg_codes,
                depart_at=path.depart_at,
                eta=eta,
                fee_cents=fee,
            ),
        ),
        eta=eta,
        fee_cents=fee,
        transfers=path.transfers,
        score=score,
        feasible=not reasons,
        rejection_reasons=tuple(reasons),
    )


def build_candidates(
    *,
    shipment_code: str,
    unit_codes: tuple[str, ...],
    origin: str,
    destination: str,
    earliest_depart: datetime,
    promise: datetime,
    temp_requirement: TempClass,
    network: TransportNetwork,
    reserved: Mapping[str, int],
    allow_split: bool = True,
) -> list[Candidate]:
    """生成并评估候选改线。

    先评估整组同车方案；若全部因容量不可行且允许拆分，则把单元分成两组，
    让最早可用的路径先走一部分，其余走次优路径，保证拆分后每组保管关系连续。
    """
    paths = network.find_paths(
        origin, destination, earliest_depart, temp_requirement
    )
    candidates = [
        evaluate_path(path, unit_codes, promise, reserved) for path in paths
    ]
    if (
        allow_split
        and len(unit_codes) > 1
        and candidates
        and not any(c.feasible for c in candidates)
    ):
        split = _split_candidate(
            paths=paths,
            unit_codes=unit_codes,
            promise=promise,
            reserved=reserved,
        )
        if split is not None:
            candidates.append(split)
    candidates.sort(
        key=lambda c: (not c.feasible, c.score, c.candidate_code)
    )
    return candidates


def _split_candidate(
    *,
    paths: list[PathOption],
    unit_codes: tuple[str, ...],
    promise: datetime,
    reserved: Mapping[str, int],
) -> Candidate | None:
    ordered = sorted(paths, key=lambda p: (p.arrive_at, p.transfers))
    for first in ordered:
        first_capacity = min(
            leg.capacity_total - reserved.get(leg.leg_code, 0) for leg in first.legs
        )
        if first_capacity < 1:
            continue
        take = min(first_capacity, len(unit_codes) - 1)
        head = tuple(sorted(unit_codes)[:take])
        tail = tuple(sorted(unit_codes)[take:])
        for second in ordered:
            if second.leg_codes == first.leg_codes:
                continue
            second_capacity = min(
                leg.capacity_total - reserved.get(leg.leg_code, 0)
                for leg in second.legs
            )
            if second_capacity < len(tail):
                continue
            head_fee = sum(leg.fee_for(len(head)) for leg in first.legs)
            tail_fee = sum(leg.fee_for(len(tail)) for leg in second.legs)
            eta = max(first.arrive_at, second.arrive_at)
            fee = head_fee + tail_fee
            transfers = first.transfers + second.transfers + 1
            score = _score(eta, promise, fee, transfers)
            code = stable_code(
                "CAND",
                "split",
                first.leg_codes,
                second.leg_codes,
                sorted(unit_codes),
            )
            return Candidate(
                candidate_code=code,
                parts=(
                    CandidatePart(
                        unit_codes=head,
                        leg_codes=first.leg_codes,
                        depart_at=first.depart_at,
                        eta=first.arrive_at,
                        fee_cents=head_fee,
                    ),
                    CandidatePart(
                        unit_codes=tail,
                        leg_codes=second.leg_codes,
                        depart_at=second.depart_at,
                        eta=second.arrive_at,
                        fee_cents=tail_fee,
                    ),
                ),
                eta=eta,
                fee_cents=fee,
                transfers=transfers,
                score=score,
                feasible=True,
                rejection_reasons=(),
            )
    return None


def compose_segments(
    *,
    leg_codes: tuple[str, ...],
    legs: Mapping[str, LegSchedule],
    unit_count: int,
    start_seq: int,
    carried_over: bool = False,
) -> tuple[PlanSegment, ...]:
    """把一串班次展开为方案路段。"""
    segments: list[PlanSegment] = []
    for offset, leg_code in enumerate(leg_codes):
        leg = legs[leg_code]
        segments.append(
            PlanSegment(
                seq=start_seq + offset,
                leg_code=leg.leg_code,
                origin=leg.origin,
                destination=leg.destination,
                planned_depart_at=leg.depart_at,
                planned_arrive_at=leg.arrive_at,
                fee_cents=leg.fee_for(unit_count),
                state=SegmentState.PENDING,
                carried_over=carried_over,
            )
        )
    return tuple(segments)
