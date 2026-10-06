"""多式联运异常重排的核心领域模型。

所有实体均为不可变数据类，状态演进通过 ``dataclasses.replace`` 产生新版本，
配合 ``serde`` 模块可直接落盘为 JSON，保证服务重启后状态完整恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LegMode(str, Enum):
    RAIL = "rail"  # 干线铁路
    COLD_TRUCK = "cold_truck"  # 末端冷链车
    TRUCK = "truck"  # 普通公路应急运力


class LegStatus(str, Enum):
    SCHEDULED = "scheduled"
    DELAYED = "delayed"
    DEPARTED = "departed"
    ARRIVED = "arrived"
    CANCELLED = "cancelled"


class UnitState(str, Enum):
    PLANNED = "planned"  # 已规划，尚未交接给承运班次
    IN_TRANSIT = "in_transit"  # 在某运输班次上
    AT_HUB = "at_hub"  # 在枢纽/仓库等待下一程
    DELIVERED = "delivered"  # 已签收
    REJECTED = "rejected"  # 被拒收，转入人工处置
    DAMAGED = "damaged"  # 已损坏，转入理赔


TERMINAL_UNIT_STATES = (UnitState.DELIVERED, UnitState.REJECTED, UnitState.DAMAGED)


class ShipmentState(str, Enum):
    PLANNED = "planned"
    IN_TRANSIT = "in_transit"
    PARTIALLY_DELIVERED = "partially_delivered"
    DELIVERED = "delivered"
    EXCEPTION = "exception"


class EventType(str, Enum):
    LOAD = "load"  # 装车/装列
    UNLOAD = "unload"  # 卸货
    HANDOVER = "handover"  # 承运方之间交接
    DELIVER = "deliver"  # 签收
    REJECT = "reject"  # 拒收
    TEMP_DEVIATION = "temp_deviation"  # 温度偏差
    DAMAGE = "damage"  # 损坏


class BookingState(str, Enum):
    RESERVED = "reserved"  # 已占位
    RELEASED = "released"  # 改线释放
    FULFILLED = "fulfilled"  # 该班次段已完成
    CANCELLED = "cancelled"  # 容量取消被逐出


class CommitmentState(str, Enum):
    ACTIVE = "active"
    VOIDED = "voided"  # 已失效（被新承诺替代）
    FULFILLED = "fulfilled"


class PlanState(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class ReplanState(str, Enum):
    PROPOSED = "proposed"  # 已生成候选，尚未冻结
    FROZEN = "frozen"  # 已冻结选定方案


class DisruptionType(str, Enum):
    HUB_CLOSED = "hub_closed"  # 枢纽封闭
    LEG_DELAYED = "leg_delayed"  # 班次延误
    CAPACITY_CANCELLED = "capacity_cancelled"  # 容量取消
    PARTIAL_DAMAGE = "partial_damage"  # 部分损坏


class FeeKind(str, Enum):
    CHARGE = "charge"  # 预约计费
    REFUND = "refund"  # 释放退费


@dataclass(frozen=True)
class TransportLeg:
    """运输班次：具有容量、时窗和温控能力的一段运力。"""

    leg_code: str
    mode: LegMode
    origin: str
    destination: str
    depart_at: str
    arrive_at: str
    capacity: int
    temp_min: float
    temp_max: float
    fee_per_unit: float
    status: LegStatus = LegStatus.SCHEDULED

    def covers_temp(self, need_min: float, need_max: float) -> bool:
        """班次温控区间是否完全覆盖货物要求。"""
        return self.temp_min <= need_min and self.temp_max >= need_max


@dataclass(frozen=True)
class Shipment:
    """商家的一票货物承诺。"""

    shipment_code: str
    merchant: str
    origin: str
    destination: str
    temp_min: float
    temp_max: float
    unit_count: int
    promised_by: str  # 向商家承诺的最迟交付时间
    created_at: str
    state: ShipmentState = ShipmentState.PLANNED
    exception_note: str | None = None


@dataclass(frozen=True)
class CustodyRecord:
    """一段连续保管关系：某段时间内包装单元由谁持有。"""

    custodian_type: str  # node | leg | consignee
    custodian_code: str
    since: str
    until: str | None


@dataclass(frozen=True)
class PackageUnit:
    """拆分后的包装单元（托盘等），携带完整保管链。"""

    unit_code: str
    shipment_code: str
    seq: int
    state: UnitState
    location_type: str  # node | leg | consignee
    location_code: str
    created_at: str
    custody: tuple[CustodyRecord, ...] = ()
    temp_compromised: bool = False


@dataclass(frozen=True)
class Booking:
    """对某班次容量的预约占位。"""

    booking_id: str
    shipment_code: str
    leg_code: str
    unit_codes: tuple[str, ...]
    space: int
    fee: float
    state: BookingState
    created_at: str
    released_at: str | None = None
    release_reason: str | None = None


@dataclass(frozen=True)
class RouteSegment:
    leg_code: str
    origin: str
    destination: str
    depart_at: str
    arrive_at: str


@dataclass(frozen=True)
class RoutePlan:
    """一条已提交的路线方案；revision 单调递增，历史版本只读。"""

    plan_id: str
    shipment_code: str
    revision: int
    segments: tuple[RouteSegment, ...]
    eta: str
    total_fee: float
    state: PlanState
    reason: str  # 为什么走这条路线
    created_at: str


@dataclass(frozen=True)
class Commitment:
    """对商家的交付时间承诺；改线时旧承诺作废并签发新承诺。"""

    commitment_id: str
    shipment_code: str
    promised_by: str
    state: CommitmentState
    issued_at: str
    exceeds_request: bool = False  # 是否超出商家要求的最迟时间
    voided_at: str | None = None
    void_reason: str | None = None
    superseded_by: str | None = None


@dataclass(frozen=True)
class FeeEntry:
    """费用流水：预约计费与释放退费均可审计。"""

    entry_id: str
    shipment_code: str
    kind: FeeKind
    amount: float
    note: str
    at: str
    booking_id: str | None = None


@dataclass(frozen=True)
class Event:
    """运输事件。event_id 为幂等键；occurred_at 为业务发生时间。"""

    event_id: str
    type: EventType
    unit_code: str
    occurred_at: str
    received_at: str
    leg_code: str | None = None
    node: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Disruption:
    disruption_id: str
    type: DisruptionType
    target: str
    occurred_at: str
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RouteChoice:
    """候选路径（规划层输出，尚未落库）。"""

    segments: tuple[RouteSegment, ...]
    eta: str
    total_fee: float
    transfers: int
    explanation: str


@dataclass(frozen=True)
class Candidate:
    """重排记录中的候选方案，带稳定编号供人工选择。"""

    candidate_id: str
    segments: tuple[RouteSegment, ...]
    eta: str
    total_fee: float
    transfers: int
    explanation: str


@dataclass(frozen=True)
class Replan:
    """一次异常重排：触发原因、候选集合、冻结结果与人工理由。"""

    replan_id: str
    shipment_code: str
    trigger: str
    disruption_id: str | None
    unit_codes: tuple[str, ...]
    from_node: str
    state: ReplanState
    candidates: tuple[Candidate, ...]
    created_at: str
    chosen_candidate_id: str | None = None
    chosen_by: str | None = None  # auto 或操作员标识
    choose_reason: str | None = None
    frozen_at: str | None = None
    applied_plan_id: str | None = None


@dataclass(frozen=True)
class LiabilityRecord:
    """赔付责任视图：异常事件落在哪一段保管关系上。"""

    unit_code: str
    event_type: EventType
    occurred_at: str
    segment_type: str  # leg | node | consignee | unknown
    segment_code: str
    detail: str
