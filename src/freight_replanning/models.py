"""多式联运异常重排领域模型。

所有实体均为不可变对象，状态变化通过生成新版本并追加到日志完成，
时间一律使用 UTC 时区感知的 ``datetime``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any

UTC = timezone.utc


def ensure_utc(value: datetime) -> datetime:
    """把时间统一成 UTC 时区感知对象。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def fmt_dt(value: datetime) -> str:
    return ensure_utc(value).isoformat()


def parse_dt(raw: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(raw))


def stable_code(prefix: str, *parts: Any, length: int = 10) -> str:
    """根据内容生成稳定编码，供幂等键与候选方案标识使用。"""
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = sha256(payload.encode("utf-8")).hexdigest()[:length].upper()
    return f"{prefix}-{digest}"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_text(value: str, field_name: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), f"{field_name} 不能为空")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class HubKind(StrEnum):
    COUNTY_ORIGIN = "county_origin"  # 县域集货点
    RAIL_TERMINAL = "rail_terminal"  # 铁路场站
    REGIONAL_WAREHOUSE = "regional_warehouse"  # 区域仓
    CITY_STATION = "city_station"  # 末端站点


class TransportMode(StrEnum):
    RAIL = "rail"  # 干线铁路
    WAREHOUSE_SHUTTLE = "warehouse_shuttle"  # 仓间转运
    COLD_TRUCK = "cold_truck"  # 末端冷链车


class TempClass(StrEnum):
    FROZEN = "frozen"  # 冷冻
    CHILLED = "chilled"  # 冷藏
    AMBIENT = "ambient"  # 常温


class LegStatus(StrEnum):
    SCHEDULED = "scheduled"
    DELAYED = "delayed"
    CANCELLED = "cancelled"


class SegmentState(StrEnum):
    PENDING = "pending"  # 未开始
    ACTIVE = "active"  # 已装车/在途
    COMPLETED = "completed"  # 已完成（不可被重排破坏）
    RELEASED = "released"  # 被新方案释放


class PlanState(StrEnum):
    FROZEN = "frozen"  # 已冻结生效
    SUPERSEDED = "superseded"  # 被新修订版取代
    COMPLETED = "completed"  # 全部路段完成


class CommitmentKind(StrEnum):
    SEGMENT_ARRIVAL = "segment_arrival"  # 路段到达承诺
    FINAL_DELIVERY = "final_delivery"  # 最终交付承诺


class CommitmentState(StrEnum):
    ACTIVE = "active"
    MET = "met"
    LAPSED = "lapsed"  # 已失效


class UnitState(StrEnum):
    PENDING = "pending"  # 待起运
    AT_HUB = "at_hub"  # 在枢纽/仓内
    IN_TRANSIT = "in_transit"  # 在途
    DELIVERED = "delivered"  # 已签收
    REJECTED = "rejected"  # 被拒收
    DAMAGED = "damaged"  # 已报损隔离
    SPLIT = "split"  # 已拆分为子单元


TERMINAL_UNIT_STATES = frozenset(
    {UnitState.DELIVERED, UnitState.REJECTED, UnitState.DAMAGED, UnitState.SPLIT}
)


class EventType(StrEnum):
    LOAD = "load"  # 装车/装列
    UNLOAD = "unload"  # 卸车/卸列
    HANDOVER = "handover"  # 交接
    REJECTION = "rejection"  # 拒收
    TEMP_DEVIATION = "temp_deviation"  # 温度偏差
    DAMAGE = "damage"  # 损坏报告


class HolderKind(StrEnum):
    HUB = "hub"
    LEG = "leg"
    CONSIGNEE = "consignee"  # 收货人（签收终点）


class DisruptionKind(StrEnum):
    INITIAL_BOOKING = "initial_booking"  # 首次组货（非异常，用于统一记录决策）
    HUB_CLOSED = "hub_closed"  # 枢纽封闭
    HUB_REOPENED = "hub_reopened"
    LEG_DELAYED = "leg_delayed"  # 班次延误
    LEG_CANCELLED = "leg_cancelled"  # 班次取消
    CAPACITY_REDUCED = "capacity_reduced"  # 容量取消/缩减
    PARTIAL_DAMAGE = "partial_damage"  # 部分损坏


class DecisionStatus(StrEnum):
    OPEN = "open"  # 待人工处理
    RESOLVED = "resolved"  # 已选定方案


class SelectionMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# 网络与资源
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hub:
    hub_code: str
    kind: HubKind
    name: str
    temp_classes: frozenset[TempClass]
    transfer_buffer_minutes: int = 30
    is_open: bool = True

    def __post_init__(self) -> None:
        _require_text(self.hub_code, "hub_code")
        _require_text(self.name, "name")
        object.__setattr__(self, "kind", HubKind(self.kind))
        object.__setattr__(
            self, "temp_classes", frozenset(TempClass(t) for t in self.temp_classes)
        )
        _require(self.transfer_buffer_minutes >= 0, "transfer_buffer_minutes 不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "hub_code": self.hub_code,
            "kind": self.kind.value,
            "name": self.name,
            "temp_classes": sorted(t.value for t in self.temp_classes),
            "transfer_buffer_minutes": self.transfer_buffer_minutes,
            "is_open": self.is_open,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Hub":
        return cls(
            hub_code=raw["hub_code"],
            kind=HubKind(raw["kind"]),
            name=raw["name"],
            temp_classes=frozenset(TempClass(t) for t in raw["temp_classes"]),
            transfer_buffer_minutes=int(raw.get("transfer_buffer_minutes", 30)),
            is_open=bool(raw.get("is_open", True)),
        )


@dataclass(frozen=True, slots=True)
class LegSchedule:
    """运输班次：具有容量、时窗与温控能力的一段运输资源。"""

    leg_code: str
    mode: TransportMode
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime
    capacity_total: int
    temp_classes: frozenset[TempClass]
    base_fee_cents: int
    fee_per_unit_cents: int
    carrier_code: str = ""
    status: LegStatus = LegStatus.SCHEDULED

    def __post_init__(self) -> None:
        _require_text(self.leg_code, "leg_code")
        _require_text(self.origin, "origin")
        _require_text(self.destination, "destination")
        _require(self.origin != self.destination, "班次起点与终点不能相同")
        object.__setattr__(self, "mode", TransportMode(self.mode))
        object.__setattr__(self, "status", LegStatus(self.status))
        object.__setattr__(
            self, "temp_classes", frozenset(TempClass(t) for t in self.temp_classes)
        )
        object.__setattr__(self, "depart_at", ensure_utc(self.depart_at))
        object.__setattr__(self, "arrive_at", ensure_utc(self.arrive_at))
        _require(self.depart_at < self.arrive_at, "班次出发时间必须早于到达时间")
        _require(self.capacity_total >= 0, "capacity_total 不能为负")
        _require(self.base_fee_cents >= 0, "base_fee_cents 不能为负")
        _require(self.fee_per_unit_cents >= 0, "fee_per_unit_cents 不能为负")

    def fee_for(self, units: int) -> int:
        return self.base_fee_cents + self.fee_per_unit_cents * units

    def supports(self, requirement: TempClass) -> bool:
        return requirement in self.temp_classes

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_code": self.leg_code,
            "mode": self.mode.value,
            "origin": self.origin,
            "destination": self.destination,
            "depart_at": fmt_dt(self.depart_at),
            "arrive_at": fmt_dt(self.arrive_at),
            "capacity_total": self.capacity_total,
            "temp_classes": sorted(t.value for t in self.temp_classes),
            "base_fee_cents": self.base_fee_cents,
            "fee_per_unit_cents": self.fee_per_unit_cents,
            "carrier_code": self.carrier_code,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LegSchedule":
        return cls(
            leg_code=raw["leg_code"],
            mode=TransportMode(raw["mode"]),
            origin=raw["origin"],
            destination=raw["destination"],
            depart_at=parse_dt(raw["depart_at"]),
            arrive_at=parse_dt(raw["arrive_at"]),
            capacity_total=int(raw["capacity_total"]),
            temp_classes=frozenset(TempClass(t) for t in raw["temp_classes"]),
            base_fee_cents=int(raw["base_fee_cents"]),
            fee_per_unit_cents=int(raw["fee_per_unit_cents"]),
            carrier_code=raw.get("carrier_code", ""),
            status=LegStatus(raw.get("status", LegStatus.SCHEDULED.value)),
        )


# ---------------------------------------------------------------------------
# 货物与包装单元
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Shipment:
    shipment_code: str
    merchant_code: str
    goods_description: str
    temp_requirement: TempClass
    origin_hub: str
    destination_hub: str
    promised_deliver_by: datetime
    fee_cents: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_text(self.shipment_code, "shipment_code")
        _require_text(self.merchant_code, "merchant_code")
        _require_text(self.origin_hub, "origin_hub")
        _require_text(self.destination_hub, "destination_hub")
        object.__setattr__(
            self, "temp_requirement", TempClass(self.temp_requirement)
        )
        object.__setattr__(
            self, "promised_deliver_by", ensure_utc(self.promised_deliver_by)
        )
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        _require(self.fee_cents >= 0, "fee_cents 不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "shipment_code": self.shipment_code,
            "merchant_code": self.merchant_code,
            "goods_description": self.goods_description,
            "temp_requirement": self.temp_requirement.value,
            "origin_hub": self.origin_hub,
            "destination_hub": self.destination_hub,
            "promised_deliver_by": fmt_dt(self.promised_deliver_by),
            "fee_cents": self.fee_cents,
            "created_at": fmt_dt(self.created_at),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Shipment":
        return cls(
            shipment_code=raw["shipment_code"],
            merchant_code=raw["merchant_code"],
            goods_description=raw["goods_description"],
            temp_requirement=TempClass(raw["temp_requirement"]),
            origin_hub=raw["origin_hub"],
            destination_hub=raw["destination_hub"],
            promised_deliver_by=parse_dt(raw["promised_deliver_by"]),
            fee_cents=int(raw.get("fee_cents", 0)),
            created_at=parse_dt(raw["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class PackagingUnit:
    """包装单元（托盘/周转箱），拆分后通过 parent_unit_code 保持血缘。"""

    unit_code: str
    shipment_code: str
    state: UnitState = UnitState.PENDING
    parent_unit_code: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        _require_text(self.unit_code, "unit_code")
        _require_text(self.shipment_code, "shipment_code")
        object.__setattr__(self, "state", UnitState(self.state))

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_code": self.unit_code,
            "shipment_code": self.shipment_code,
            "state": self.state.value,
            "parent_unit_code": self.parent_unit_code,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PackagingUnit":
        return cls(
            unit_code=raw["unit_code"],
            shipment_code=raw["shipment_code"],
            state=UnitState(raw.get("state", UnitState.PENDING.value)),
            parent_unit_code=raw.get("parent_unit_code"),
            note=raw.get("note", ""),
        )


# ---------------------------------------------------------------------------
# 方案、路段与承诺
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanSegment:
    seq: int
    leg_code: str
    origin: str
    destination: str
    planned_depart_at: datetime
    planned_arrive_at: datetime
    fee_cents: int
    state: SegmentState = SegmentState.PENDING
    carried_over: bool = False  # 从上一修订版保留的已完成/在途路段

    def __post_init__(self) -> None:
        _require(self.seq >= 1, "seq 必须从 1 开始")
        _require_text(self.leg_code, "leg_code")
        object.__setattr__(self, "state", SegmentState(self.state))
        object.__setattr__(
            self, "planned_depart_at", ensure_utc(self.planned_depart_at)
        )
        object.__setattr__(
            self, "planned_arrive_at", ensure_utc(self.planned_arrive_at)
        )
        _require(self.fee_cents >= 0, "fee_cents 不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "leg_code": self.leg_code,
            "origin": self.origin,
            "destination": self.destination,
            "planned_depart_at": fmt_dt(self.planned_depart_at),
            "planned_arrive_at": fmt_dt(self.planned_arrive_at),
            "fee_cents": self.fee_cents,
            "state": self.state.value,
            "carried_over": self.carried_over,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PlanSegment":
        return cls(
            seq=int(raw["seq"]),
            leg_code=raw["leg_code"],
            origin=raw["origin"],
            destination=raw["destination"],
            planned_depart_at=parse_dt(raw["planned_depart_at"]),
            planned_arrive_at=parse_dt(raw["planned_arrive_at"]),
            fee_cents=int(raw["fee_cents"]),
            state=SegmentState(raw.get("state", SegmentState.PENDING.value)),
            carried_over=bool(raw.get("carried_over", False)),
        )


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """货运承诺组合：一组包装单元在一串班次上的冻结方案。"""

    plan_code: str
    shipment_code: str
    revision: int
    unit_codes: tuple[str, ...]
    segments: tuple[PlanSegment, ...]
    fee_cents: int
    eta: datetime
    rationale: str = ""
    state: PlanState = PlanState.FROZEN
    decision_code: str | None = None
    parent_plan_code: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    created_by: str = "system"

    def __post_init__(self) -> None:
        _require_text(self.plan_code, "plan_code")
        _require_text(self.shipment_code, "shipment_code")
        _require(self.revision >= 1, "revision 必须从 1 开始")
        _require(bool(self.unit_codes), "方案至少覆盖一个包装单元")
        object.__setattr__(self, "unit_codes", tuple(self.unit_codes))
        object.__setattr__(self, "segments", tuple(self.segments))
        object.__setattr__(self, "state", PlanState(self.state))
        object.__setattr__(self, "eta", ensure_utc(self.eta))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        _require(self.fee_cents >= 0, "fee_cents 不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_code": self.plan_code,
            "shipment_code": self.shipment_code,
            "revision": self.revision,
            "unit_codes": list(self.unit_codes),
            "segments": [s.to_dict() for s in self.segments],
            "fee_cents": self.fee_cents,
            "eta": fmt_dt(self.eta),
            "rationale": self.rationale,
            "state": self.state.value,
            "decision_code": self.decision_code,
            "parent_plan_code": self.parent_plan_code,
            "created_at": fmt_dt(self.created_at),
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RoutePlan":
        return cls(
            plan_code=raw["plan_code"],
            shipment_code=raw["shipment_code"],
            revision=int(raw["revision"]),
            unit_codes=tuple(raw["unit_codes"]),
            segments=tuple(PlanSegment.from_dict(s) for s in raw["segments"]),
            fee_cents=int(raw["fee_cents"]),
            eta=parse_dt(raw["eta"]),
            rationale=raw.get("rationale", ""),
            state=PlanState(raw.get("state", PlanState.FROZEN.value)),
            decision_code=raw.get("decision_code"),
            parent_plan_code=raw.get("parent_plan_code"),
            created_at=parse_dt(raw["created_at"]),
            created_by=raw.get("created_by", "system"),
        )


@dataclass(frozen=True, slots=True)
class Commitment:
    commitment_code: str
    shipment_code: str
    plan_code: str
    plan_revision: int
    kind: CommitmentKind
    description: str
    due_at: datetime
    segment_seq: int | None = None
    state: CommitmentState = CommitmentState.ACTIVE
    lapsed_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.commitment_code, "commitment_code")
        _require_text(self.shipment_code, "shipment_code")
        object.__setattr__(self, "kind", CommitmentKind(self.kind))
        object.__setattr__(self, "state", CommitmentState(self.state))
        object.__setattr__(self, "due_at", ensure_utc(self.due_at))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        if self.closed_at is not None:
            object.__setattr__(self, "closed_at", ensure_utc(self.closed_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "commitment_code": self.commitment_code,
            "shipment_code": self.shipment_code,
            "plan_code": self.plan_code,
            "plan_revision": self.plan_revision,
            "kind": self.kind.value,
            "description": self.description,
            "due_at": fmt_dt(self.due_at),
            "segment_seq": self.segment_seq,
            "state": self.state.value,
            "lapsed_reason": self.lapsed_reason,
            "created_at": fmt_dt(self.created_at),
            "closed_at": fmt_dt(self.closed_at) if self.closed_at else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Commitment":
        return cls(
            commitment_code=raw["commitment_code"],
            shipment_code=raw["shipment_code"],
            plan_code=raw["plan_code"],
            plan_revision=int(raw["plan_revision"]),
            kind=CommitmentKind(raw["kind"]),
            description=raw["description"],
            due_at=parse_dt(raw["due_at"]),
            segment_seq=raw.get("segment_seq"),
            state=CommitmentState(raw.get("state", CommitmentState.ACTIVE.value)),
            lapsed_reason=raw.get("lapsed_reason"),
            created_at=parse_dt(raw["created_at"]),
            closed_at=parse_dt(raw["closed_at"]) if raw.get("closed_at") else None,
        )


@dataclass(frozen=True, slots=True)
class FeeAdjustment:
    adjustment_code: str
    shipment_code: str
    old_fee_cents: int
    new_fee_cents: int
    reason: str
    plan_code: str
    created_at: datetime

    @property
    def delta_cents(self) -> int:
        return self.new_fee_cents - self.old_fee_cents

    def to_dict(self) -> dict[str, Any]:
        return {
            "adjustment_code": self.adjustment_code,
            "shipment_code": self.shipment_code,
            "old_fee_cents": self.old_fee_cents,
            "new_fee_cents": self.new_fee_cents,
            "reason": self.reason,
            "plan_code": self.plan_code,
            "created_at": fmt_dt(self.created_at),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FeeAdjustment":
        return cls(
            adjustment_code=raw["adjustment_code"],
            shipment_code=raw["shipment_code"],
            old_fee_cents=int(raw["old_fee_cents"]),
            new_fee_cents=int(raw["new_fee_cents"]),
            reason=raw["reason"],
            plan_code=raw["plan_code"],
            created_at=parse_dt(raw["created_at"]),
        )


# ---------------------------------------------------------------------------
# 重排候选与决策
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidatePart:
    """候选方案中一组包装单元走的一串班次。"""

    unit_codes: tuple[str, ...]
    leg_codes: tuple[str, ...]
    depart_at: datetime
    eta: datetime
    fee_cents: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit_codes", tuple(self.unit_codes))
        object.__setattr__(self, "leg_codes", tuple(self.leg_codes))
        object.__setattr__(self, "depart_at", ensure_utc(self.depart_at))
        object.__setattr__(self, "eta", ensure_utc(self.eta))

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_codes": list(self.unit_codes),
            "leg_codes": list(self.leg_codes),
            "depart_at": fmt_dt(self.depart_at),
            "eta": fmt_dt(self.eta),
            "fee_cents": self.fee_cents,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CandidatePart":
        return cls(
            unit_codes=tuple(raw["unit_codes"]),
            leg_codes=tuple(raw["leg_codes"]),
            depart_at=parse_dt(raw["depart_at"]),
            eta=parse_dt(raw["eta"]),
            fee_cents=int(raw["fee_cents"]),
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_code: str
    parts: tuple[CandidatePart, ...]
    eta: datetime
    fee_cents: int
    transfers: int
    score: float
    feasible: bool
    rejection_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        object.__setattr__(self, "eta", ensure_utc(self.eta))
        object.__setattr__(self, "rejection_reasons", tuple(self.rejection_reasons))

    @property
    def is_split(self) -> bool:
        return len(self.parts) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_code": self.candidate_code,
            "parts": [p.to_dict() for p in self.parts],
            "eta": fmt_dt(self.eta),
            "fee_cents": self.fee_cents,
            "transfers": self.transfers,
            "score": round(self.score, 4),
            "feasible": self.feasible,
            "is_split": self.is_split,
            "rejection_reasons": list(self.rejection_reasons),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Candidate":
        return cls(
            candidate_code=raw["candidate_code"],
            parts=tuple(CandidatePart.from_dict(p) for p in raw["parts"]),
            eta=parse_dt(raw["eta"]),
            fee_cents=int(raw["fee_cents"]),
            transfers=int(raw["transfers"]),
            score=float(raw["score"]),
            feasible=bool(raw["feasible"]),
            rejection_reasons=tuple(raw.get("rejection_reasons", ())),
        )


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    """一次重排决策：触发原因、候选比较、选定结果与人工理由。"""

    decision_code: str
    shipment_code: str
    trigger_kind: DisruptionKind
    trigger_detail: dict[str, Any]
    candidates: tuple[Candidate, ...]
    status: DecisionStatus = DecisionStatus.OPEN
    selected_candidate_code: str | None = None
    selection_mode: SelectionMode | None = None
    operator: str | None = None
    operator_reason: str | None = None
    resulting_plan_codes: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.decision_code, "decision_code")
        _require_text(self.shipment_code, "shipment_code")
        object.__setattr__(self, "trigger_kind", DisruptionKind(self.trigger_kind))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "status", DecisionStatus(self.status))
        if self.selection_mode is not None:
            object.__setattr__(
                self, "selection_mode", SelectionMode(self.selection_mode)
            )
        object.__setattr__(self, "resulting_plan_codes", tuple(self.resulting_plan_codes))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        if self.decided_at is not None:
            object.__setattr__(self, "decided_at", ensure_utc(self.decided_at))
        if self.status == DecisionStatus.RESOLVED:
            _require(
                self.selected_candidate_code is not None, "已决策记录必须包含选定候选"
            )
            if self.selection_mode == SelectionMode.MANUAL:
                _require(
                    bool(self.operator_reason and self.operator_reason.strip()),
                    "人工强制选择必须留下理由",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_code": self.decision_code,
            "shipment_code": self.shipment_code,
            "trigger_kind": self.trigger_kind.value,
            "trigger_detail": self.trigger_detail,
            "candidates": [c.to_dict() for c in self.candidates],
            "status": self.status.value,
            "selected_candidate_code": self.selected_candidate_code,
            "selection_mode": self.selection_mode.value if self.selection_mode else None,
            "operator": self.operator,
            "operator_reason": self.operator_reason,
            "resulting_plan_codes": list(self.resulting_plan_codes),
            "created_at": fmt_dt(self.created_at),
            "decided_at": fmt_dt(self.decided_at) if self.decided_at else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReplanDecision":
        return cls(
            decision_code=raw["decision_code"],
            shipment_code=raw["shipment_code"],
            trigger_kind=DisruptionKind(raw["trigger_kind"]),
            trigger_detail=dict(raw.get("trigger_detail", {})),
            candidates=tuple(Candidate.from_dict(c) for c in raw["candidates"]),
            status=DecisionStatus(raw.get("status", DecisionStatus.OPEN.value)),
            selected_candidate_code=raw.get("selected_candidate_code"),
            selection_mode=(
                SelectionMode(raw["selection_mode"]) if raw.get("selection_mode") else None
            ),
            operator=raw.get("operator"),
            operator_reason=raw.get("operator_reason"),
            resulting_plan_codes=tuple(raw.get("resulting_plan_codes", ())),
            created_at=parse_dt(raw["created_at"]),
            decided_at=parse_dt(raw["decided_at"]) if raw.get("decided_at") else None,
        )


# ---------------------------------------------------------------------------
# 保管链与责任
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CustodyObservation:
    """保管期间发生的温度偏差/损坏等观察记录。"""

    event_id: str
    event_type: EventType
    occurred_at: datetime
    detail: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", EventType(self.event_type))
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "occurred_at": fmt_dt(self.occurred_at),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CustodyObservation":
        return cls(
            event_id=raw["event_id"],
            event_type=EventType(raw["event_type"]),
            occurred_at=parse_dt(raw["occurred_at"]),
            detail=dict(raw.get("detail", {})),
        )


@dataclass(frozen=True, slots=True)
class CustodySpan:
    """一段连续保管关系：某包装单元在某持有者手中的一段时间。"""

    unit_code: str
    holder_kind: HolderKind
    holder_code: str
    started_at: datetime
    start_event_id: str
    ended_at: datetime | None = None
    end_event_id: str | None = None
    start_gap: bool = False  # 起点与上一段保管不衔接（事件缺失）
    observations: tuple[CustodyObservation, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.unit_code, "unit_code")
        _require_text(self.holder_code, "holder_code")
        object.__setattr__(self, "holder_kind", HolderKind(self.holder_kind))
        object.__setattr__(self, "started_at", ensure_utc(self.started_at))
        if self.ended_at is not None:
            object.__setattr__(self, "ended_at", ensure_utc(self.ended_at))
        object.__setattr__(self, "observations", tuple(self.observations))

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_code": self.unit_code,
            "holder_kind": self.holder_kind.value,
            "holder_code": self.holder_code,
            "started_at": fmt_dt(self.started_at),
            "start_event_id": self.start_event_id,
            "ended_at": fmt_dt(self.ended_at) if self.ended_at else None,
            "end_event_id": self.end_event_id,
            "start_gap": self.start_gap,
            "observations": [o.to_dict() for o in self.observations],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CustodySpan":
        return cls(
            unit_code=raw["unit_code"],
            holder_kind=HolderKind(raw["holder_kind"]),
            holder_code=raw["holder_code"],
            started_at=parse_dt(raw["started_at"]),
            start_event_id=raw["start_event_id"],
            ended_at=parse_dt(raw["ended_at"]) if raw.get("ended_at") else None,
            end_event_id=raw.get("end_event_id"),
            start_gap=bool(raw.get("start_gap", False)),
            observations=tuple(
                CustodyObservation.from_dict(o) for o in raw.get("observations", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class LiabilityRecord:
    """赔付责任判定：异常事件落在哪一段保管/运输上。"""

    shipment_code: str
    unit_code: str
    cause: EventType
    responsible_party: str
    holder_kind: HolderKind
    holder_code: str
    detail: str
    evidence_event_ids: tuple[str, ...]
    assessed_at: datetime
    plan_code: str | None = None
    segment_seq: int | None = None
    leg_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cause", EventType(self.cause))
        object.__setattr__(self, "holder_kind", HolderKind(self.holder_kind))
        object.__setattr__(self, "evidence_event_ids", tuple(self.evidence_event_ids))
        object.__setattr__(self, "assessed_at", ensure_utc(self.assessed_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "shipment_code": self.shipment_code,
            "unit_code": self.unit_code,
            "cause": self.cause.value,
            "responsible_party": self.responsible_party,
            "holder_kind": self.holder_kind.value,
            "holder_code": self.holder_code,
            "detail": self.detail,
            "evidence_event_ids": list(self.evidence_event_ids),
            "assessed_at": fmt_dt(self.assessed_at),
            "plan_code": self.plan_code,
            "segment_seq": self.segment_seq,
            "leg_code": self.leg_code,
        }


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogisticsEvent:
    """装卸、交接、拒收、温度偏差等运输事件。

    ``event_id`` 是幂等键：重复到达的事件只生效一次；
    ``occurred_at`` 是业务发生时间，乱序到达时按它归位。
    """

    event_id: str
    event_type: EventType
    unit_code: str
    occurred_at: datetime
    leg_code: str | None = None
    hub_code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id")
        _require_text(self.unit_code, "unit_code")
        object.__setattr__(self, "event_type", EventType(self.event_type))
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "payload", dict(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "unit_code": self.unit_code,
            "occurred_at": fmt_dt(self.occurred_at),
            "leg_code": self.leg_code,
            "hub_code": self.hub_code,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LogisticsEvent":
        return cls(
            event_id=raw["event_id"],
            event_type=EventType(raw["event_type"]),
            unit_code=raw["unit_code"],
            occurred_at=parse_dt(raw["occurred_at"]),
            leg_code=raw.get("leg_code"),
            hub_code=raw.get("hub_code"),
            payload=dict(raw.get("payload", {})),
        )


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """已入账事件：附带接收序号（乱序归位依据）与归段结果。"""

    event: LogisticsEvent
    seq: int
    received_at: datetime
    status: str = "accepted"  # accepted / rejected
    reason: str | None = None
    attribution: dict[str, Any] | None = None  # {plan_code, segment_seq, leg_code}

    def __post_init__(self) -> None:
        object.__setattr__(self, "received_at", ensure_utc(self.received_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "seq": self.seq,
            "received_at": fmt_dt(self.received_at),
            "status": self.status,
            "reason": self.reason,
            "attribution": self.attribution,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StoredEvent":
        return cls(
            event=LogisticsEvent.from_dict(raw["event"]),
            seq=int(raw["seq"]),
            received_at=parse_dt(raw["received_at"]),
            status=raw.get("status", "accepted"),
            reason=raw.get("reason"),
            attribution=dict(raw["attribution"]) if raw.get("attribution") else None,
        )


@dataclass(frozen=True, slots=True)
class Reservation:
    """班次容量占位记录，key 在方案谱系内稳定，保证重试幂等。"""

    key: str
    plan_code: str
    leg_code: str
    segment_seq: int
    units: int

    def __post_init__(self) -> None:
        _require_text(self.key, "key")
        _require(self.units >= 0, "units 不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "plan_code": self.plan_code,
            "leg_code": self.leg_code,
            "segment_seq": self.segment_seq,
            "units": self.units,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reservation":
        return cls(
            key=raw["key"],
            plan_code=raw["plan_code"],
            leg_code=raw["leg_code"],
            segment_seq=int(raw["segment_seq"]),
            units=int(raw["units"]),
        )
