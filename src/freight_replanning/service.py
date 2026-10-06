"""多式联运异常重排的应用服务层。

职责：
- 注册班次与货物，组合初始货运承诺；
- 接收装卸、交接、拒收、温度偏差等事件，幂等且容忍乱序地归入正确运输段；
- 在枢纽封闭、延误、容量取消、部分损坏时比较候选改线并冻结选定方案，
  同步调整剩余预约、费用与承诺时间，且不破坏已完成路段；
- 提供"货物在哪、为什么走这条路线、哪些承诺已失效、赔付责任在哪一段"的查询。

所有修改都在 ``JsonStore.transact`` 的独占锁内完成，并发重排不会重复占位。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

from .models import (
    TERMINAL_UNIT_STATES,
    Booking,
    BookingState,
    Candidate,
    Commitment,
    CommitmentState,
    CustodyRecord,
    Disruption,
    DisruptionType,
    Event,
    EventType,
    FeeEntry,
    FeeKind,
    LegMode,
    LegStatus,
    LiabilityRecord,
    PackageUnit,
    PlanState,
    Replan,
    ReplanState,
    RouteChoice,
    RoutePlan,
    RouteSegment,
    Shipment,
    ShipmentState,
    TransportLeg,
    UnitState,
)
from .planning import find_candidate_paths, free_capacity
from .serde import from_jsonable, to_jsonable
from .store import JsonStore
from .timeutil import now_iso, parse, plus_minutes


class CapacityConflict(RuntimeError):
    """占位时容量不足（并发竞争或候选失效），调用方可改试其他候选。"""


@dataclass
class World:
    """一次事务内可变的全量状态视图。"""

    legs: dict[str, TransportLeg] = field(default_factory=dict)
    shipments: dict[str, Shipment] = field(default_factory=dict)
    units: dict[str, PackageUnit] = field(default_factory=dict)
    bookings: dict[str, Booking] = field(default_factory=dict)
    plans: dict[str, RoutePlan] = field(default_factory=dict)
    commitments: dict[str, Commitment] = field(default_factory=dict)
    fees: list[FeeEntry] = field(default_factory=list)
    events: dict[str, Event] = field(default_factory=dict)
    disruptions: dict[str, Disruption] = field(default_factory=dict)
    replans: dict[str, Replan] = field(default_factory=dict)
    closed_hubs: list[str] = field(default_factory=list)
    pending_events: list[str] = field(default_factory=list)  # 暂不能归段的事件（乱序待补齐）
    counters: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, state: dict) -> "World":
        def load(key: str, cls_: type) -> dict:
            return {k: from_jsonable(cls_, v) for k, v in state.get(key, {}).items()}

        return cls(
            legs=load("legs", TransportLeg),
            shipments=load("shipments", Shipment),
            units=load("units", PackageUnit),
            bookings=load("bookings", Booking),
            plans=load("plans", RoutePlan),
            commitments=load("commitments", Commitment),
            fees=[from_jsonable(FeeEntry, f) for f in state.get("fees", [])],
            events=load("events", Event),
            disruptions=load("disruptions", Disruption),
            replans=load("replans", Replan),
            closed_hubs=list(state.get("closed_hubs", [])),
            pending_events=list(state.get("pending_events", [])),
            counters=dict(state.get("counters", {})),
        )

    def to_dict(self) -> dict:
        return {
            "legs": {k: to_jsonable(v) for k, v in self.legs.items()},
            "shipments": {k: to_jsonable(v) for k, v in self.shipments.items()},
            "units": {k: to_jsonable(v) for k, v in self.units.items()},
            "bookings": {k: to_jsonable(v) for k, v in self.bookings.items()},
            "plans": {k: to_jsonable(v) for k, v in self.plans.items()},
            "commitments": {k: to_jsonable(v) for k, v in self.commitments.items()},
            "fees": [to_jsonable(f) for f in self.fees],
            "events": {k: to_jsonable(v) for k, v in self.events.items()},
            "disruptions": {k: to_jsonable(v) for k, v in self.disruptions.items()},
            "replans": {k: to_jsonable(v) for k, v in self.replans.items()},
            "closed_hubs": list(self.closed_hubs),
            "pending_events": list(self.pending_events),
            "counters": dict(self.counters),
        }

    def snapshot(self) -> dict:
        """浅拷贝各集合，用于候选占位失败时在同一事务内回滚。"""
        return {
            "legs": dict(self.legs),
            "shipments": dict(self.shipments),
            "units": dict(self.units),
            "bookings": dict(self.bookings),
            "plans": dict(self.plans),
            "commitments": dict(self.commitments),
            "fees": list(self.fees),
            "events": dict(self.events),
            "disruptions": dict(self.disruptions),
            "replans": dict(self.replans),
            "closed_hubs": list(self.closed_hubs),
            "pending_events": list(self.pending_events),
            "counters": dict(self.counters),
        }

    def restore(self, snap: dict) -> None:
        for key, value in snap.items():
            current = getattr(self, key)
            current.clear()
            if isinstance(current, list):
                current.extend(value)
            else:
                current.update(value)


def _next_id(world: World, prefix: str) -> str:
    n = int(world.counters.get(prefix, 0)) + 1
    world.counters[prefix] = n
    return f"{prefix}-{n:04d}"


def _event_order(ev: Event) -> tuple:
    return (ev.occurred_at, ev.received_at, ev.event_id)


class FreightService:
    """多式联运异常重排服务入口。"""

    def __init__(self, store_dir: str):
        self.store = JsonStore(store_dir)

    # ------------------------------------------------------------------
    # 事务与查询基座
    # ------------------------------------------------------------------
    def _tx(self, fn: Callable[[World], object]) -> object:
        def apply(state: dict) -> object:
            world = World.from_dict(state)
            result = fn(world)
            new_state = world.to_dict()
            new_state["version"] = state.get("version", 0)
            state.clear()
            state.update(new_state)
            return result

        return self.store.transact(apply)

    def _view(self, fn: Callable[[World], object]) -> object:
        return fn(World.from_dict(self.store.load()))

    # ------------------------------------------------------------------
    # 注册班次与货物
    # ------------------------------------------------------------------
    def register_leg(
        self,
        *,
        leg_code: str,
        mode: str,
        origin: str,
        destination: str,
        depart_at: str,
        arrive_at: str,
        capacity: int,
        temp_min: float,
        temp_max: float,
        fee_per_unit: float,
    ) -> TransportLeg:
        def op(world: World) -> TransportLeg:
            if leg_code in world.legs:
                raise ValueError(f"班次 {leg_code} 已存在")
            if parse(depart_at) >= parse(arrive_at):
                raise ValueError("出发时间必须早于到达时间")
            if int(capacity) < 1:
                raise ValueError("容量必须大于零")
            if float(temp_min) > float(temp_max):
                raise ValueError("温控区间不合法")
            leg = TransportLeg(
                leg_code=leg_code,
                mode=LegMode(mode),
                origin=origin,
                destination=destination,
                depart_at=depart_at,
                arrive_at=arrive_at,
                capacity=int(capacity),
                temp_min=float(temp_min),
                temp_max=float(temp_max),
                fee_per_unit=float(fee_per_unit),
            )
            world.legs[leg_code] = leg
            return leg

        return self._tx(op)  # type: ignore[return-value]

    def register_shipment(
        self,
        *,
        shipment_code: str,
        merchant: str,
        origin: str,
        destination: str,
        unit_count: int,
        temp_min: float,
        temp_max: float,
        promised_by: str,
    ) -> dict:
        """登记货物并自动组合初始路线、预约与承诺。"""

        def op(world: World) -> dict:
            if shipment_code in world.shipments:
                raise ValueError(f"货物 {shipment_code} 已存在")
            if int(unit_count) < 1:
                raise ValueError("包装单元数量必须大于零")
            parse(promised_by)  # 校验时间格式
            now = now_iso()
            shipment = Shipment(
                shipment_code=shipment_code,
                merchant=merchant,
                origin=origin,
                destination=destination,
                temp_min=float(temp_min),
                temp_max=float(temp_max),
                unit_count=int(unit_count),
                promised_by=promised_by,
                created_at=now,
            )
            unit_codes = []
            for seq in range(1, int(unit_count) + 1):
                unit_code = f"{shipment_code}-U{seq:03d}"
                world.units[unit_code] = PackageUnit(
                    unit_code=unit_code,
                    shipment_code=shipment_code,
                    seq=seq,
                    state=UnitState.PLANNED,
                    location_type="node",
                    location_code=origin,
                    created_at=now,
                    custody=(CustodyRecord("node", origin, now, None),),
                )
                unit_codes.append(unit_code)
            world.shipments[shipment_code] = shipment
            choices = find_candidate_paths(
                legs=world.legs.values(),
                bookings=world.bookings.values(),
                start_node=origin,
                destination=destination,
                not_before=now,
                space_needed=int(unit_count),
                temp_min=float(temp_min),
                temp_max=float(temp_max),
                closed_hubs=world.closed_hubs,
            )
            if not choices:
                raise ValueError("初始无可行路径，请先补充运输班次")
            plan = self._activate_route(
                world, shipment, tuple(unit_codes), choices[0], reason_prefix="初始规划"
            )
            return {"shipment": shipment, "unit_codes": unit_codes, "plan": plan}

        return self._tx(op)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 事件接收：幂等、容忍乱序、归入正确运输段
    # ------------------------------------------------------------------
    def ingest_event(
        self,
        *,
        event_id: str,
        type: str,
        unit_code: str,
        occurred_at: str,
        leg_code: str | None = None,
        node: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        def op(world: World) -> dict:
            if event_id in world.events:
                return {"status": "duplicate", "event_id": event_id}
            if unit_code not in world.units:
                raise KeyError(f"包装单元 {unit_code} 不存在")
            parse(occurred_at)
            event = Event(
                event_id=event_id,
                type=EventType(type),
                unit_code=unit_code,
                occurred_at=occurred_at,
                received_at=now_iso(),
                leg_code=leg_code,
                node=node,
                payload=dict(payload or {}),
            )
            unit = world.units[unit_code]
            shipment = world.shipments[unit.shipment_code]
            world.events[event_id] = event
            applied, reason = self._try_replay(world, shipment, unit)
            self._after_unit_change(world, shipment.shipment_code)
            if not applied:
                return {
                    "status": "pending",
                    "event_id": event_id,
                    "reason": reason,
                    "hint": "事件已登记，待缺失的前序事件到达后自动归段",
                }
            new_unit = world.units[unit_code]
            return {
                "status": "applied",
                "event_id": event_id,
                "unit_state": new_unit.state.value,
                "location": {"type": new_unit.location_type, "code": new_unit.location_code},
                "segment": self._attribute(new_unit, event),
            }

        return self._tx(op)  # type: ignore[return-value]

    def _try_replay(self, world: World, shipment: Shipment, unit: PackageUnit) -> tuple[bool, str]:
        """用该单元的全部非挂起事件 + 新事件重放。

        成功则更新单元并清除挂起标记；失败则把不可归段的事件挂起，
        单元保持上一致状态，等待乱序的前序事件补齐。
        """
        events = sorted(
            (e for e in world.events.values() if e.unit_code == unit.unit_code),
            key=_event_order,
        )
        try:
            new_unit = self._replay_unit(shipment, unit, world.legs, events)
        except ValueError as exc:
            pending = set(world.pending_events)
            # 逐个定位仍不可归段的事件（通常是最后到达的乱序事件）
            applicable: list[Event] = []
            for ev in events:
                try:
                    self._replay_unit(shipment, unit, world.legs, applicable + [ev])
                    applicable.append(ev)
                    pending.discard(ev.event_id)
                except ValueError:
                    pending.add(ev.event_id)
            world.pending_events = sorted(pending)
            # 用可归段的事件前缀推进单元状态，其余保持挂起
            world.units[unit.unit_code] = self._replay_unit(shipment, unit, world.legs, applicable)
            return False, str(exc)
        world.units[unit.unit_code] = new_unit
        world.pending_events = [
            eid for eid in world.pending_events
            if world.events[eid].unit_code != unit.unit_code
        ]
        return True, ""

    def _replay_unit(
        self,
        shipment: Shipment,
        unit: PackageUnit,
        legs: dict[str, TransportLeg],
        events: list[Event],
    ) -> PackageUnit:
        """按发生时间重放单元全部事件，重建保管链与当前状态。

        乱序到达的事件按 occurred_at 归位，因此重复/迟到事件都能归入正确运输段。
        """
        state = UnitState.PLANNED
        loc_type, loc_code = "node", shipment.origin
        custody: list[CustodyRecord] = [CustodyRecord("node", shipment.origin, unit.created_at, None)]
        temp_compromised = False

        def close_and_open(new_type: str, new_code: str, at: str) -> None:
            custody[-1] = replace(custody[-1], until=at)
            custody.append(CustodyRecord(new_type, new_code, at, None))

        for ev in events:
            t = ev.occurred_at
            if ev.type == EventType.LOAD:
                leg = legs.get(ev.leg_code or "")
                if leg is None:
                    raise ValueError(f"事件 {ev.event_id} 引用了不存在的班次 {ev.leg_code}")
                if loc_type != "node" or loc_code != leg.origin:
                    raise ValueError(
                        f"事件 {ev.event_id} 与保管链矛盾：单元在 {loc_type}:{loc_code}，"
                        f"无法装上从 {leg.origin} 出发的班次 {leg.leg_code}"
                    )
                close_and_open("leg", leg.leg_code, t)
                loc_type, loc_code = "leg", leg.leg_code
                state = UnitState.IN_TRANSIT
            elif ev.type == EventType.UNLOAD:
                if loc_type != "leg":
                    raise ValueError(f"事件 {ev.event_id} 与保管链矛盾：单元不在运输班次上，无法卸货")
                leg = legs[loc_code]
                node = ev.node or leg.destination
                if node != leg.destination:
                    raise ValueError(
                        f"事件 {ev.event_id} 卸货节点 {node} 与班次 {leg.leg_code} 终点 {leg.destination} 不符"
                    )
                close_and_open("node", node, t)
                loc_type, loc_code = "node", node
                state = UnitState.AT_HUB
            elif ev.type == EventType.HANDOVER:
                to_leg = ev.payload.get("to_leg")
                to_node = ev.payload.get("to_node") or ev.node
                if to_leg:
                    leg = legs.get(to_leg)
                    if leg is None:
                        raise ValueError(f"事件 {ev.event_id} 引用了不存在的班次 {to_leg}")
                    current_node = loc_code if loc_type == "node" else legs[loc_code].destination
                    if current_node != leg.origin:
                        raise ValueError(
                            f"事件 {ev.event_id} 与保管链矛盾：无法在 {current_node} 交接给从 {leg.origin} 出发的班次"
                        )
                    close_and_open("leg", to_leg, t)
                    loc_type, loc_code = "leg", to_leg
                    state = UnitState.IN_TRANSIT
                elif to_node:
                    close_and_open("node", to_node, t)
                    loc_type, loc_code = "node", to_node
                    state = UnitState.AT_HUB
                else:
                    raise ValueError(f"交接事件 {ev.event_id} 缺少 to_leg 或 to_node")
            elif ev.type == EventType.DELIVER:
                close_and_open("consignee", shipment.destination, t)
                loc_type, loc_code = "consignee", shipment.destination
                state = UnitState.DELIVERED
            elif ev.type == EventType.REJECT:
                state = UnitState.REJECTED
            elif ev.type == EventType.DAMAGE:
                state = UnitState.DAMAGED
            elif ev.type == EventType.TEMP_DEVIATION:
                temp_compromised = True
        return replace(
            unit,
            state=state,
            location_type=loc_type,
            location_code=loc_code,
            custody=tuple(custody),
            temp_compromised=temp_compromised,
        )

    def _attribute(self, unit: PackageUnit, event: Event) -> dict:
        """把事件归到发生时刻所在的保管段；拒收归到最后承运的班次。"""
        record = self._custody_at(unit.custody, event.occurred_at)
        if event.type == EventType.REJECT and record.custodian_type != "leg":
            leg_records = [r for r in unit.custody if r.custodian_type == "leg"]
            if leg_records:
                record = leg_records[-1]
        return {"segment_type": record.custodian_type, "segment_code": record.custodian_code}

    @staticmethod
    def _custody_at(custody: Iterable[CustodyRecord], at: str) -> CustodyRecord:
        chosen: CustodyRecord | None = None
        for record in custody:
            if record.since <= at and (record.until is None or at < record.until):
                chosen = record
        if chosen is None:
            chain = list(custody)
            chosen = chain[-1] if chain else CustodyRecord("unknown", "unknown", at, None)
        return chosen

    def _after_unit_change(self, world: World, shipment_code: str) -> None:
        """事件落库后同步派生状态：完成预约、兑现承诺、刷新货物状态。"""
        units = self._units_of(world, shipment_code)
        for booking in world.bookings.values():
            if booking.shipment_code != shipment_code or booking.state != BookingState.RESERVED:
                continue
            if all(self._unit_completed_leg(world.units[u], booking.leg_code) for u in booking.unit_codes):
                world.bookings[booking.booking_id] = replace(booking, state=BookingState.FULFILLED)
        if units and all(u.state == UnitState.DELIVERED for u in units):
            for commitment in world.commitments.values():
                if commitment.shipment_code == shipment_code and commitment.state == CommitmentState.ACTIVE:
                    world.commitments[commitment.commitment_id] = replace(
                        commitment, state=CommitmentState.FULFILLED
                    )
        self._refresh_shipment_state(world, shipment_code)
        # 到达枢纽后若无可用续程预约（如续程班次被取消），立即触发重排
        shipment = world.shipments[shipment_code]
        for unit in self._units_of(world, shipment_code):
            if unit.state != UnitState.AT_HUB:
                continue
            if unit.location_code == shipment.destination:
                continue  # 已到目的地枢纽，等待签收
            has_onward = any(
                b.state == BookingState.RESERVED
                and unit.unit_code in b.unit_codes
                and world.legs[b.leg_code].origin == unit.location_code
                and world.legs[b.leg_code].status != LegStatus.CANCELLED
                for b in world.bookings.values()
            )
            if not has_onward:
                self._replan_shipment(
                    world, shipment_code, f"到达 {unit.location_code} 后无可用续程预约", None
                )
                break

    @staticmethod
    def _unit_completed_leg(unit: PackageUnit, leg_code: str) -> bool:
        return any(
            r.custodian_type == "leg" and r.custodian_code == leg_code and r.until is not None
            for r in unit.custody
        )

    @staticmethod
    def _units_of(world: World, shipment_code: str) -> list[PackageUnit]:
        return [u for u in world.units.values() if u.shipment_code == shipment_code]

    def _refresh_shipment_state(self, world: World, shipment_code: str) -> None:
        shipment = world.shipments[shipment_code]
        units = self._units_of(world, shipment_code)
        states = [u.state for u in units]
        if states and all(s == UnitState.DELIVERED for s in states):
            new_state = ShipmentState.DELIVERED
        elif states and all(s in TERMINAL_UNIT_STATES for s in states):
            # 部分签收、部分拒收/损坏：整票不失败，如实呈现部分交付
            new_state = ShipmentState.PARTIALLY_DELIVERED
        elif any(s == UnitState.DELIVERED for s in states):
            new_state = ShipmentState.PARTIALLY_DELIVERED
        elif shipment.exception_note:
            new_state = ShipmentState.EXCEPTION
        elif any(s in (UnitState.IN_TRANSIT, UnitState.AT_HUB) for s in states):
            new_state = ShipmentState.IN_TRANSIT
        else:
            new_state = ShipmentState.PLANNED
        world.shipments[shipment_code] = replace(shipment, state=new_state)

    # ------------------------------------------------------------------
    # 路线激活：释放旧预约 → 占位 → 换计划 → 换承诺 → 费用流水
    # ------------------------------------------------------------------
    def _activate_route(
        self,
        world: World,
        shipment: Shipment,
        unit_codes: tuple[str, ...],
        choice: RouteChoice | Candidate,
        *,
        reason_prefix: str,
    ) -> RoutePlan:
        now = now_iso()
        unit_set = set(unit_codes)
        # 1. 释放这些单元尚未履行的预约（已完成路段的预约已 FULFILLED，不受影响）
        self._release_future_bookings(world, shipment.shipment_code, unit_set, now, "改线释放")
        # 2. 逐段校验容量并占位；并发下容量不足则抛 CapacityConflict，事务回滚
        for seg in choice.segments:
            leg = world.legs[seg.leg_code]
            if leg.status == LegStatus.CANCELLED:
                raise CapacityConflict(f"班次 {leg.leg_code} 已取消")
            if free_capacity(leg, world.bookings.values()) < len(unit_codes):
                raise CapacityConflict(f"班次 {leg.leg_code} 剩余容量不足")
        for seg in choice.segments:
            self._assert_no_overlap(world, unit_set, seg)
            booking_id = _next_id(world, "BK")
            leg = world.legs[seg.leg_code]
            fee = round(len(unit_codes) * leg.fee_per_unit, 2)
            world.bookings[booking_id] = Booking(
                booking_id=booking_id,
                shipment_code=shipment.shipment_code,
                leg_code=seg.leg_code,
                unit_codes=tuple(unit_codes),
                space=len(unit_codes),
                fee=fee,
                state=BookingState.RESERVED,
                created_at=now,
            )
            world.fees.append(
                FeeEntry(
                    entry_id=_next_id(world, "FE"),
                    shipment_code=shipment.shipment_code,
                    kind=FeeKind.CHARGE,
                    amount=fee,
                    note=f"预约班次 {seg.leg_code}（{len(unit_codes)} 件）",
                    at=now,
                    booking_id=booking_id,
                )
            )
        # 3. 计划版本更替
        for plan in list(world.plans.values()):
            if plan.shipment_code == shipment.shipment_code and plan.state == PlanState.ACTIVE:
                world.plans[plan.plan_id] = replace(plan, state=PlanState.SUPERSEDED)
        revision = 1 + max(
            (p.revision for p in world.plans.values() if p.shipment_code == shipment.shipment_code),
            default=0,
        )
        plan = RoutePlan(
            plan_id=_next_id(world, "PLAN"),
            shipment_code=shipment.shipment_code,
            revision=revision,
            segments=tuple(choice.segments),
            eta=choice.eta,
            total_fee=round(float(choice.total_fee), 2),
            state=PlanState.ACTIVE,
            reason=f"{reason_prefix}：{choice.explanation}",
            created_at=now,
        )
        world.plans[plan.plan_id] = plan
        # 4. 承诺调整：旧承诺作废，签发新承诺
        self._reissue_commitment(world, shipment, choice.eta, now, reason_prefix)
        # 5. 异常解除
        if shipment.exception_note:
            world.shipments[shipment.shipment_code] = replace(shipment, exception_note=None)
        self._refresh_shipment_state(world, shipment.shipment_code)
        return plan

    def _release_future_bookings(
        self, world: World, shipment_code: str, unit_set: set[str], now: str, reason: str
    ) -> None:
        """释放或缩减指定单元的有效预约；已完成（FULFILLED）预约保持不动。"""
        for booking in list(world.bookings.values()):
            if booking.shipment_code != shipment_code or booking.state != BookingState.RESERVED:
                continue
            involved = [u for u in booking.unit_codes if u in unit_set]
            if not involved:
                continue
            # 单元仍在此班次上（尚未卸货）：保留该段占位，待到达后再处理
            if any(
                world.units[u].location_type == "leg"
                and world.units[u].location_code == booking.leg_code
                for u in involved
            ):
                continue
            others = [u for u in booking.unit_codes if u not in unit_set]
            others_active = [
                u for u in others if world.units[u].state not in TERMINAL_UNIT_STATES
            ]
            if not others_active:
                self._release_booking(world, booking, now, reason)
            else:
                # 同一预约里还有其他在途单元：只拆分出本次改线的部分
                released_space = len(booking.unit_codes) - len(others_active)
                refund = round(booking.fee * released_space / len(booking.unit_codes), 2)
                world.bookings[booking.booking_id] = replace(
                    booking,
                    unit_codes=tuple(others_active),
                    space=len(others_active),
                    fee=round(booking.fee - refund, 2),
                )
                world.fees.append(
                    FeeEntry(
                        entry_id=_next_id(world, "FE"),
                        shipment_code=shipment_code,
                        kind=FeeKind.REFUND,
                        amount=refund,
                        note=f"{reason}：拆分释放班次 {booking.leg_code} 部分占位",
                        at=now,
                        booking_id=booking.booking_id,
                    )
                )

    def _release_booking(self, world: World, booking: Booking, now: str, reason: str) -> None:
        world.bookings[booking.booking_id] = replace(
            booking,
            state=BookingState.RELEASED,
            released_at=now,
            release_reason=reason,
        )
        world.fees.append(
            FeeEntry(
                entry_id=_next_id(world, "FE"),
                shipment_code=booking.shipment_code,
                kind=FeeKind.REFUND,
                amount=booking.fee,
                note=f"{reason}：释放班次 {booking.leg_code} 预约 {booking.booking_id}",
                at=now,
                booking_id=booking.booking_id,
            )
        )

    @staticmethod
    def _assert_no_overlap(world: World, unit_set: set[str], seg: RouteSegment) -> None:
        """安全网：同一单元不得在两辆时间重叠的承运班次上同时占位。"""
        new_start, new_end = parse(seg.depart_at), parse(seg.arrive_at)
        for other in world.bookings.values():
            if other.state != BookingState.RESERVED or other.leg_code == seg.leg_code:
                continue
            if not unit_set.intersection(other.unit_codes):
                continue
            other_leg = world.legs[other.leg_code]
            if parse(other_leg.depart_at) < new_end and new_start < parse(other_leg.arrive_at):
                raise CapacityConflict(
                    f"单元在班次 {other.leg_code} 与 {seg.leg_code} 上时间重叠占位"
                )

    def _reissue_commitment(
        self, world: World, shipment: Shipment, eta: str, now: str, reason: str
    ) -> Commitment:
        old = next(
            (
                c
                for c in world.commitments.values()
                if c.shipment_code == shipment.shipment_code and c.state == CommitmentState.ACTIVE
            ),
            None,
        )
        if old and old.promised_by == eta:
            return old
        new_id = _next_id(world, "CMT")
        if old:
            world.commitments[old.commitment_id] = replace(
                old,
                state=CommitmentState.VOIDED,
                voided_at=now,
                void_reason=reason,
                superseded_by=new_id,
            )
        commitment = Commitment(
            commitment_id=new_id,
            shipment_code=shipment.shipment_code,
            promised_by=eta,
            state=CommitmentState.ACTIVE,
            issued_at=now,
            exceeds_request=parse(eta) > parse(shipment.promised_by),
        )
        world.commitments[new_id] = commitment
        return commitment

    # ------------------------------------------------------------------
    # 异常上报与重排
    # ------------------------------------------------------------------
    def report_disruption(
        self,
        *,
        type: str,
        target: str,
        occurred_at: str | None = None,
        details: dict | None = None,
        disruption_id: str | None = None,
    ) -> dict:
        raw_details = dict(details or {})

        def op(world: World) -> dict:
            dtype = DisruptionType(type)
            did = disruption_id or _next_id(world, "DS")
            if did in world.disruptions:
                return {"disruption_id": did, "status": "duplicate"}
            occurred = occurred_at or now_iso()
            details = dict(raw_details)
            world.disruptions[did] = Disruption(did, dtype, target, occurred, details)
            if dtype == DisruptionType.HUB_CLOSED:
                result = self._handle_hub_closed(world, target, did)
            elif dtype == DisruptionType.LEG_DELAYED:
                result = self._handle_leg_delayed(world, target, did, details)
            elif dtype == DisruptionType.CAPACITY_CANCELLED:
                result = self._handle_capacity_cancelled(world, target, did, details)
            elif dtype == DisruptionType.PARTIAL_DAMAGE:
                result = self._handle_partial_damage(world, target, did, occurred, details)
            else:  # pragma: no cover - 枚举已穷尽
                raise ValueError(f"未知异常类型 {type}")
            return {"disruption_id": did, "status": "processed", **result}

        return self._tx(op)  # type: ignore[return-value]

    def _handle_hub_closed(self, world: World, node: str, did: str) -> dict:
        if node not in world.closed_hubs:
            world.closed_hubs.append(node)
        affected = []
        replans = []
        for code in self._shipments_through_node(world, node):
            affected.append(code)
            replans.extend(self._replan_shipment(world, code, f"枢纽 {node} 封闭", did))
        return {"affected_shipments": affected, "replans": replans}

    def _shipments_through_node(self, world: World, node: str) -> list[str]:
        codes = []
        for shipment in world.shipments.values():
            units = [u for u in self._units_of(world, shipment.shipment_code)
                     if u.state not in TERMINAL_UNIT_STATES]
            if not units:
                continue
            if any(u.location_type == "node" and u.location_code == node for u in units):
                codes.append(shipment.shipment_code)
                continue
            plan = self._active_plan(world, shipment.shipment_code)
            if plan and any(
                node in (seg.origin, seg.destination)
                and not all(self._unit_completed_leg(world.units[u], seg.leg_code) for u in
                            [x.unit_code for x in units])
                for seg in plan.segments
            ):
                codes.append(shipment.shipment_code)
        return sorted(codes)

    def _handle_leg_delayed(self, world: World, leg_code: str, did: str, details: dict) -> dict:
        leg = world.legs.get(leg_code)
        if leg is None:
            raise KeyError(f"班次 {leg_code} 不存在")
        minutes = float(details.get("delay_minutes", 0))
        new_depart = details.get("depart_at") or plus_minutes(leg.depart_at, minutes)
        new_arrive = details.get("arrive_at") or plus_minutes(leg.arrive_at, minutes)
        world.legs[leg_code] = replace(
            leg, depart_at=new_depart, arrive_at=new_arrive, status=LegStatus.DELAYED
        )
        affected, replans, adjusted = [], [], []
        shipment_codes = sorted(
            {b.shipment_code for b in world.bookings.values()
             if b.leg_code == leg_code and b.state == BookingState.RESERVED}
        )
        for code in shipment_codes:
            on_leg = [
                u for u in self._units_of(world, code)
                if u.location_type == "leg" and u.location_code == leg_code
            ]
            affected.append(code)
            if on_leg:
                # 货物已在班次上：不扯下在途货物，仅顺延计划与承诺
                self._shift_plan_and_commitment(world, code, leg_code, new_depart, new_arrive, minutes)
                adjusted.append(code)
            else:
                replans.extend(self._replan_shipment(world, code, f"班次 {leg_code} 延误", did))
        return {"affected_shipments": affected, "replans": replans, "commitment_adjusted": adjusted}

    def _shift_plan_and_commitment(
        self, world: World, shipment_code: str, leg_code: str,
        new_depart: str, new_arrive: str, minutes: float,
    ) -> None:
        plan = self._active_plan(world, shipment_code)
        if plan and any(seg.leg_code == leg_code for seg in plan.segments):
            segments = tuple(
                replace(seg, depart_at=new_depart, arrive_at=new_arrive)
                if seg.leg_code == leg_code else seg
                for seg in plan.segments
            )
            eta = plus_minutes(plan.eta, minutes) if minutes else segments[-1].arrive_at
            world.plans[plan.plan_id] = replace(plan, segments=segments, eta=eta)
            shipment = world.shipments[shipment_code]
            self._reissue_commitment(
                world, shipment, eta, now_iso(), f"班次 {leg_code} 延误 {minutes:g} 分钟"
            )

    def _handle_capacity_cancelled(self, world: World, leg_code: str, did: str, details: dict) -> dict:
        leg = world.legs.get(leg_code)
        if leg is None:
            raise KeyError(f"班次 {leg_code} 不存在")
        if details.get("cancel_leg"):
            world.legs[leg_code] = replace(leg, status=LegStatus.CANCELLED)
        else:
            amount = int(details.get("amount", 0))
            world.legs[leg_code] = replace(leg, capacity=max(0, leg.capacity - amount))
        now = now_iso()
        evicted: list[str] = []
        while True:  # 超售时按占位时间从晚到早逐出，直到不再超售；整班取消则全部逐出
            reserved = [
                b for b in world.bookings.values()
                if b.leg_code == leg_code and b.state == BookingState.RESERVED
            ]
            effective_capacity = (
                0 if world.legs[leg_code].status == LegStatus.CANCELLED
                else world.legs[leg_code].capacity
            )
            if sum(b.space for b in reserved) <= effective_capacity:
                break
            victim = max(reserved, key=lambda b: (b.created_at, b.booking_id))
            world.bookings[victim.booking_id] = replace(
                victim, state=BookingState.CANCELLED, released_at=now,
                release_reason=f"班次 {leg_code} 容量取消被逐出",
            )
            world.fees.append(
                FeeEntry(
                    entry_id=_next_id(world, "FE"),
                    shipment_code=victim.shipment_code,
                    kind=FeeKind.REFUND,
                    amount=victim.fee,
                    note=f"班次 {leg_code} 容量取消，预约 {victim.booking_id} 退费",
                    at=now,
                    booking_id=victim.booking_id,
                )
            )
            evicted.append(victim.shipment_code)
        replans = []
        for code in sorted(set(evicted)):
            replans.extend(self._replan_shipment(world, code, f"班次 {leg_code} 容量取消", did))
        return {"affected_shipments": sorted(set(evicted)), "replans": replans}

    def _handle_partial_damage(
        self, world: World, target: str, did: str, occurred: str, details: dict
    ) -> dict:
        unit_codes = list(details.get("unit_codes", []))
        note = str(details.get("note", ""))
        shipment_codes: set[str] = set()
        for unit_code in unit_codes:
            unit = world.units.get(unit_code)
            if unit is None:
                raise KeyError(f"包装单元 {unit_code} 不存在")
            event_id = f"{did}:{unit_code}"
            if event_id not in world.events:
                event = Event(
                    event_id=event_id,
                    type=EventType.DAMAGE,
                    unit_code=unit_code,
                    occurred_at=occurred,
                    received_at=now_iso(),
                    payload={"note": note, "disruption_id": did},
                )
                shipment = world.shipments[unit.shipment_code]
                timeline = sorted(
                    [e for e in world.events.values() if e.unit_code == unit_code] + [event],
                    key=_event_order,
                )
                world.units[unit_code] = self._replay_unit(shipment, unit, world.legs, timeline)
                world.events[event_id] = event
            shipment_codes.add(unit.shipment_code)
        now = now_iso()
        for code in shipment_codes:
            # 损坏单元不再旅行：释放其在各班次上的未来占位并退费
            damaged = {
                u for u in unit_codes
                if world.units[u].shipment_code == code and world.units[u].state == UnitState.DAMAGED
            }
            if damaged:
                self._release_future_bookings(world, code, damaged, now, "部分损坏释放")
        replans = []
        if details.get("blocks_current_leg"):
            for code in sorted(shipment_codes):
                replans.extend(self._replan_shipment(world, code, "部分损坏且当前班次无法继续", did))
        for code in shipment_codes:
            self._after_unit_change(world, code)
        return {"affected_shipments": sorted(shipment_codes), "replans": replans}

    # ------------------------------------------------------------------
    # 重排核心
    # ------------------------------------------------------------------
    def _replan_shipment(
        self, world: World, shipment_code: str, trigger: str, disruption_id: str | None
    ) -> list[dict]:
        shipment = world.shipments[shipment_code]
        remaining = [
            u for u in self._units_of(world, shipment_code)
            if u.state not in TERMINAL_UNIT_STATES
        ]
        if not remaining:
            return []
        groups: dict[tuple[str, str], list[PackageUnit]] = {}
        for unit in remaining:
            groups.setdefault((unit.location_type, unit.location_code), []).append(unit)
        results = []
        for (loc_type, loc_code), group in sorted(groups.items()):
            if loc_type == "leg":
                # 在途单元不可改线，等待到达下一节点后再处理
                results.append({
                    "skipped": True,
                    "reason": "单元在途，待到达下一节点后方可改线",
                    "unit_codes": [u.unit_code for u in group],
                })
                continue
            unit_codes = tuple(sorted(u.unit_code for u in group))
            existing = next(
                (
                    r for r in world.replans.values()
                    if r.shipment_code == shipment_code
                    and r.disruption_id == disruption_id
                    and r.from_node == loc_code
                    and set(r.unit_codes) == set(unit_codes)
                ),
                None,
            )
            if existing is not None:  # 同一异常重复触发：幂等返回
                results.append(to_jsonable(existing))
                continue
            results.append(
                to_jsonable(self._replan_group(world, shipment, unit_codes, loc_code, trigger, disruption_id))
            )
        return results

    def _replan_group(
        self,
        world: World,
        shipment: Shipment,
        unit_codes: tuple[str, ...],
        from_node: str,
        trigger: str,
        disruption_id: str | None,
    ) -> Replan:
        now = now_iso()
        unit_set = set(unit_codes)
        # 搜索时把即将释放的本方占位视为可用容量
        releasable_ids = {
            b.booking_id
            for b in world.bookings.values()
            if b.shipment_code == shipment.shipment_code
            and b.state == BookingState.RESERVED
            and set(b.unit_codes).issubset(
                unit_set | {u.unit_code for u in self._units_of(world, shipment.shipment_code)
                            if u.state in TERMINAL_UNIT_STATES}
            )
        }
        bookings_for_search = [
            b for b in world.bookings.values() if b.booking_id not in releasable_ids
        ]
        choices = find_candidate_paths(
            legs=world.legs.values(),
            bookings=bookings_for_search,
            start_node=from_node,
            destination=shipment.destination,
            not_before=now,
            space_needed=len(unit_codes),
            temp_min=shipment.temp_min,
            temp_max=shipment.temp_max,
            closed_hubs=world.closed_hubs,
        )
        replan_id = _next_id(world, "RP")
        candidates = tuple(
            Candidate(
                candidate_id=f"{replan_id}-C{index}",
                segments=choice.segments,
                eta=choice.eta,
                total_fee=choice.total_fee,
                transfers=choice.transfers,
                explanation=choice.explanation,
            )
            for index, choice in enumerate(choices, start=1)
        )
        replan = Replan(
            replan_id=replan_id,
            shipment_code=shipment.shipment_code,
            trigger=trigger,
            disruption_id=disruption_id,
            unit_codes=unit_codes,
            from_node=from_node,
            state=ReplanState.PROPOSED,
            candidates=candidates,
            created_at=now,
        )
        world.replans[replan_id] = replan
        for candidate in candidates:
            snap = world.snapshot()
            try:
                plan = self._activate_route(
                    world, shipment, unit_codes, candidate, reason_prefix=f"自动改线（{trigger}）"
                )
            except CapacityConflict:
                world.restore(snap)  # 并发下候选被抢占，改试下一条
                continue
            world.replans[replan_id] = replace(
                replan,
                state=ReplanState.FROZEN,
                chosen_candidate_id=candidate.candidate_id,
                chosen_by="auto",
                choose_reason="系统自动选择评分最高的候选方案",
                frozen_at=now,
                applied_plan_id=plan.plan_id,
            )
            return world.replans[replan_id]
        world.shipments[shipment.shipment_code] = replace(
            world.shipments[shipment.shipment_code],
            exception_note=f"{trigger}：暂无可行改线方案，等待人工处理或容量恢复",
        )
        self._refresh_shipment_state(world, shipment.shipment_code)
        return world.replans[replan_id]

    def choose_candidate(
        self, *, replan_id: str, candidate_id: str, operator: str, reason: str
    ) -> dict:
        """人工强制选择候选方案；必须留下理由，全程可审计。"""

        def op(world: World) -> dict:
            replan = world.replans.get(replan_id)
            if replan is None:
                raise KeyError(f"重排 {replan_id} 不存在")
            if not reason or not reason.strip():
                raise ValueError("人工强制选择必须填写理由")
            candidate = next((c for c in replan.candidates if c.candidate_id == candidate_id), None)
            if candidate is None:
                raise KeyError(f"候选 {candidate_id} 不在重排 {replan_id} 中")
            if replan.state == ReplanState.FROZEN and replan.chosen_candidate_id == candidate_id:
                return to_jsonable(world.replans[replan_id])  # 幂等
            units = [world.units[u] for u in replan.unit_codes]
            if any(u.state in TERMINAL_UNIT_STATES for u in units):
                raise ValueError("部分单元已签收/拒收/损坏，无法再改线")
            if any(u.location_type != "node" or u.location_code != replan.from_node for u in units):
                raise ValueError("单元位置已变化，无法按原重排候选改线，请重新触发重排")
            shipment = world.shipments[replan.shipment_code]
            now = now_iso()
            plan = self._activate_route(
                world, shipment, replan.unit_codes, candidate,
                reason_prefix=f"人工改线（{operator}：{reason.strip()}）",
            )
            world.replans[replan_id] = replace(
                replan,
                state=ReplanState.FROZEN,
                chosen_candidate_id=candidate_id,
                chosen_by=operator,
                choose_reason=reason.strip(),
                frozen_at=now,
                applied_plan_id=plan.plan_id,
            )
            return to_jsonable(world.replans[replan_id])

        return self._tx(op)  # type: ignore[return-value]

    def refresh_replan(self, *, replan_id: str) -> dict:
        """对尚未冻结的重排重新搜索候选（例如补充运力后）。"""

        def op(world: World) -> dict:
            replan = world.replans.get(replan_id)
            if replan is None:
                raise KeyError(f"重排 {replan_id} 不存在")
            if replan.state == ReplanState.FROZEN:
                return to_jsonable(replan)
            shipment = world.shipments[replan.shipment_code]
            units = [world.units[u] for u in replan.unit_codes]
            if any(u.location_type != "node" or u.location_code != replan.from_node for u in units):
                raise ValueError("单元位置已变化，请按最新异常重新触发重排")
            fresh = self._replan_group(
                world, shipment, replan.unit_codes, replan.from_node,
                replan.trigger, replan.disruption_id,
            )
            del world.replans[replan_id]  # 旧的重排记录被新搜索取代
            return to_jsonable(fresh)

        return self._tx(op)  # type: ignore[return-value]

    @staticmethod
    def _active_plan(world: World, shipment_code: str) -> RoutePlan | None:
        return next(
            (
                p
                for p in world.plans.values()
                if p.shipment_code == shipment_code and p.state == PlanState.ACTIVE
            ),
            None,
        )

    # ------------------------------------------------------------------
    # 查询：在哪、为什么、哪些承诺失效、赔付责任在哪段
    # ------------------------------------------------------------------
    def shipment_status(self, shipment_code: str) -> dict:
        def view(world: World) -> dict:
            shipment = world.shipments.get(shipment_code)
            if shipment is None:
                raise KeyError(f"货物 {shipment_code} 不存在")
            units = sorted(self._units_of(world, shipment_code), key=lambda u: u.seq)
            plan = self._active_plan(world, shipment_code)
            commitments = sorted(
                (c for c in world.commitments.values() if c.shipment_code == shipment_code),
                key=lambda c: c.issued_at,
            )
            fees = [f for f in world.fees if f.shipment_code == shipment_code]
            charged = round(sum(f.amount for f in fees if f.kind == FeeKind.CHARGE), 2)
            refunded = round(sum(f.amount for f in fees if f.kind == FeeKind.REFUND), 2)
            replans = sorted(
                (r for r in world.replans.values() if r.shipment_code == shipment_code),
                key=lambda r: r.created_at,
            )
            return {
                "shipment_code": shipment_code,
                "merchant": shipment.merchant,
                "state": shipment.state.value,
                "exception_note": shipment.exception_note,
                "promised_by": shipment.promised_by,
                "where": [
                    {
                        "unit_code": u.unit_code,
                        "state": u.state.value,
                        "location_type": u.location_type,
                        "location_code": u.location_code,
                        "temp_compromised": u.temp_compromised,
                    }
                    for u in units
                ],
                "route": to_jsonable(plan) if plan else None,
                "commitments": [to_jsonable(c) for c in commitments],
                "voided_commitments": [
                    to_jsonable(c) for c in commitments if c.state == CommitmentState.VOIDED
                ],
                "fees": {
                    "charged": charged,
                    "refunded": refunded,
                    "net": round(charged - refunded, 2),
                    "entries": [to_jsonable(f) for f in fees],
                },
                "liability": [to_jsonable(r) for r in self._liability(world, shipment_code)],
                "replans": [to_jsonable(r) for r in replans],
            }

        return self._view(view)  # type: ignore[return-value]

    def unit_custody(self, unit_code: str) -> dict:
        def view(world: World) -> dict:
            unit = world.units.get(unit_code)
            if unit is None:
                raise KeyError(f"包装单元 {unit_code} 不存在")
            chain = list(unit.custody)
            continuous = all(
                chain[i].until is not None and chain[i].until == chain[i + 1].since
                for i in range(len(chain) - 1)
            )
            events = sorted(
                (e for e in world.events.values() if e.unit_code == unit_code), key=_event_order
            )
            pending = set(world.pending_events)
            return {
                "unit_code": unit_code,
                "shipment_code": unit.shipment_code,
                "state": unit.state.value,
                "location": {"type": unit.location_type, "code": unit.location_code},
                "custody_continuous": continuous,
                "custody": [to_jsonable(r) for r in chain],
                "events": [
                    {**to_jsonable(e), "pending": e.event_id in pending} for e in events
                ],
            }

        return self._view(view)  # type: ignore[return-value]

    def _liability(self, world: World, shipment_code: str) -> list[LiabilityRecord]:
        """赔付责任：损坏/拒收/温度偏差归到发生时刻所在的保管段。"""
        records: list[LiabilityRecord] = []
        units = {u.unit_code: u for u in self._units_of(world, shipment_code)}
        for event in sorted(world.events.values(), key=_event_order):
            if event.unit_code not in units:
                continue
            if event.type not in (EventType.DAMAGE, EventType.REJECT, EventType.TEMP_DEVIATION):
                continue
            segment = self._attribute(units[event.unit_code], event)
            detail = str(event.payload.get("note") or "")
            if event.type == EventType.TEMP_DEVIATION and "temperature" in event.payload:
                detail = (detail + " " if detail else "") + f"实测温度 {event.payload['temperature']}℃"
            records.append(
                LiabilityRecord(
                    unit_code=event.unit_code,
                    event_type=event.type,
                    occurred_at=event.occurred_at,
                    segment_type=segment["segment_type"],
                    segment_code=segment["segment_code"],
                    detail=detail or event.type.value,
                )
            )
        return records

    def list_legs(self) -> list[dict]:
        return self._view(lambda w: [to_jsonable(l) for l in sorted(w.legs.values(), key=lambda x: x.leg_code)])  # type: ignore[return-value]

    def list_shipments(self) -> list[dict]:
        def view(world: World) -> list[dict]:
            return [
                {
                    "shipment_code": s.shipment_code,
                    "merchant": s.merchant,
                    "state": s.state.value,
                    "promised_by": s.promised_by,
                    "exception_note": s.exception_note,
                }
                for s in sorted(world.shipments.values(), key=lambda x: x.shipment_code)
            ]

        return self._view(view)  # type: ignore[return-value]

    def list_replans(self, shipment_code: str | None = None) -> list[dict]:
        def view(world: World) -> list[dict]:
            replans = sorted(world.replans.values(), key=lambda r: r.created_at)
            return [
                to_jsonable(r)
                for r in replans
                if shipment_code is None or r.shipment_code == shipment_code
            ]

        return self._view(view)  # type: ignore[return-value]

    def get_replan(self, replan_id: str) -> dict:
        def view(world: World) -> dict:
            replan = world.replans.get(replan_id)
            if replan is None:
                raise KeyError(f"重排 {replan_id} 不存在")
            return to_jsonable(replan)

        return self._view(view)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 重启恢复：扫描未交接货物，继续处理
    # ------------------------------------------------------------------
    def recover(self) -> dict:
        """服务重启后调用：刷新派生状态并列出仍需跟进的工作。"""

        def op(world: World) -> dict:
            # 重试挂起事件：重启后若前序事件已补齐，乱序事件自动归段
            for unit_code in sorted(world.units):
                unit = world.units[unit_code]
                if any(world.events[eid].unit_code == unit_code for eid in world.pending_events):
                    self._try_replay(world, world.shipments[unit.shipment_code], unit)
            for code in list(world.shipments):
                self._refresh_shipment_state(world, code)
            unhanded = [
                {
                    "unit_code": u.unit_code,
                    "shipment_code": u.shipment_code,
                    "state": u.state.value,
                    "location": {"type": u.location_type, "code": u.location_code},
                }
                for u in sorted(world.units.values(), key=lambda x: x.unit_code)
                if u.state not in TERMINAL_UNIT_STATES
            ]
            reserved = [
                to_jsonable(b)
                for b in sorted(world.bookings.values(), key=lambda x: x.booking_id)
                if b.state == BookingState.RESERVED
            ]
            open_replans = [
                to_jsonable(r)
                for r in sorted(world.replans.values(), key=lambda x: x.created_at)
                if r.state == ReplanState.PROPOSED
            ]
            return {
                "unhanded_units": unhanded,
                "reserved_bookings": reserved,
                "open_replans": open_replans,
                "pending_events": list(world.pending_events),
                "shipments": {s.shipment_code: s.state.value for s in world.shipments.values()},
            }

        return self._tx(op)  # type: ignore[return-value]
