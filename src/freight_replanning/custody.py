"""包装单元保管链构建。

把同一单元的全部事件按业务发生时间（``occurred_at``，同刻按接收序号）排序后
重放，得到连续的保管段序列。重复事件（同 ``event_id``）在入账前已被去重；
内容冗余的转移（如重复卸车）在重放时被忽略；真正的冲突
（如同一托盘在上一班次未结束时被装上另一辆车）抛出 :class:`CustodyConflict`。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from .errors import CustodyConflict, ValidationError
from .models import (
    CustodyObservation,
    CustodySpan,
    EventType,
    HolderKind,
    LogisticsEvent,
    StoredEvent,
)

_OBSERVATION_TYPES = {EventType.TEMP_DEVIATION, EventType.DAMAGE}


def validate_event_shape(event: LogisticsEvent) -> None:
    """按事件类型校验必填字段。"""
    et = event.event_type
    if et in (EventType.LOAD, EventType.UNLOAD):
        if not event.leg_code:
            raise ValidationError(f"{et.value} 事件必须携带 leg_code")
    if et == EventType.HANDOVER:
        to_kind = event.payload.get("to_holder_kind")
        to_code = event.payload.get("to_holder_code")
        if not to_kind or not to_code:
            raise ValidationError("handover 事件必须携带 to_holder_kind 与 to_holder_code")
        HolderKind(to_kind)  # 非法取值会抛出 ValueError
    if et == EventType.TEMP_DEVIATION:
        if "temperature_celsius" not in event.payload:
            raise ValidationError("temp_deviation 事件必须携带 temperature_celsius")
    if et == EventType.REJECTION:
        if not (event.leg_code or event.hub_code):
            raise ValidationError("rejection 事件必须携带 leg_code 或 hub_code")


class CustodyChain:
    """一个包装单元的完整保管链。"""

    def __init__(
        self,
        unit_code: str,
        spans: list[CustodySpan],
        orphan_observations: list[CustodyObservation],
        rejected_events: list[str],
    ) -> None:
        self.unit_code = unit_code
        self.spans = spans
        self.orphan_observations = orphan_observations
        self.rejected_events = rejected_events

    @property
    def current(self) -> CustodySpan | None:
        return self.spans[-1] if self.spans else None

    def holder_at(self, when) -> CustodySpan | None:
        for span in self.spans:
            if span.started_at <= when and (span.ended_at is None or when < span.ended_at):
                return span
        return None

    def to_dict(self) -> dict:
        return {
            "unit_code": self.unit_code,
            "spans": [s.to_dict() for s in self.spans],
            "orphan_observations": [o.to_dict() for o in self.orphan_observations],
            "rejected_events": list(self.rejected_events),
        }


def _close(span: CustodySpan, event: LogisticsEvent) -> CustodySpan:
    return replace(span, ended_at=event.occurred_at, end_event_id=event.event_id)


def _observe(span: CustodySpan, event: LogisticsEvent) -> CustodySpan:
    observation = CustodyObservation(
        event_id=event.event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        detail=dict(event.payload),
    )
    return replace(span, observations=span.observations + (observation,))


def build_custody(
    unit_code: str,
    stored_events: Iterable[StoredEvent],
    *,
    leg_origins: dict[str, str] | None = None,
    leg_destinations: dict[str, str] | None = None,
) -> CustodyChain:
    """把一个单元的全部已接受事件重放为保管链。

    :param leg_origins/leg_destinations: 班次起讫映射，用于补全装卸事件的枢纽。
    """
    leg_origins = leg_origins or {}
    leg_destinations = leg_destinations or {}
    ordered = sorted(
        (se for se in stored_events if se.status == "accepted"),
        key=lambda se: (se.event.occurred_at, se.seq, se.event.event_id),
    )
    spans: list[CustodySpan] = []
    orphans: list[CustodyObservation] = []
    rejected: list[str] = []

    def current() -> CustodySpan | None:
        return spans[-1] if spans else None

    def open_span(
        holder_kind: HolderKind,
        holder_code: str,
        event: LogisticsEvent,
        *,
        gap: bool,
    ) -> None:
        spans.append(
            CustodySpan(
                unit_code=unit_code,
                holder_kind=holder_kind,
                holder_code=holder_code,
                started_at=event.occurred_at,
                start_event_id=event.event_id,
                start_gap=gap,
            )
        )

    for stored in ordered:
        event = stored.event
        et = event.event_type
        span = current()

        if et in _OBSERVATION_TYPES:
            if span is not None and span.is_open:
                spans[-1] = _observe(span, event)
            else:
                orphans.append(
                    CustodyObservation(
                        event_id=event.event_id,
                        event_type=et,
                        occurred_at=event.occurred_at,
                        detail=dict(event.payload),
                    )
                )
            continue

        if et == EventType.REJECTION:
            # 拒收不改变保管位置，只作为观察记录挂在当前段上。
            if span is not None and span.is_open:
                spans[-1] = _observe(span, event)
            else:
                orphans.append(
                    CustodyObservation(
                        event_id=event.event_id,
                        event_type=et,
                        occurred_at=event.occurred_at,
                        detail=dict(event.payload),
                    )
                )
            continue

        if et == EventType.LOAD:
            leg = event.leg_code or ""
            if span is not None and span.is_open:
                if span.holder_kind == HolderKind.LEG:
                    if span.holder_code == leg:
                        continue  # 内容冗余的重复装车
                    raise CustodyConflict(
                        f"包装单元 {unit_code} 仍在班次 {span.holder_code} 上，"
                        f"不能同时装上班次 {leg}（同一托盘不能分给两辆车）",
                        detail={"unit_code": unit_code, "event_id": event.event_id},
                    )
                if span.holder_kind == HolderKind.CONSIGNEE:
                    raise CustodyConflict(
                        f"包装单元 {unit_code} 已签收，不能再装上班次 {leg}",
                        detail={"unit_code": unit_code, "event_id": event.event_id},
                    )
                gap = span.holder_code != leg_origins.get(leg, event.hub_code)
                spans[-1] = _close(span, event)
                open_span(HolderKind.LEG, leg, event, gap=gap)
            else:
                open_span(HolderKind.LEG, leg, event, gap=True)
            continue

        if et == EventType.UNLOAD:
            leg = event.leg_code or ""
            hub = event.hub_code or leg_destinations.get(leg, "")
            if not hub:
                raise ValidationError(
                    f"unload 事件 {event.event_id} 无法确定卸货枢纽"
                )
            if span is not None and span.is_open:
                if span.holder_kind == HolderKind.LEG:
                    if span.holder_code != leg:
                        raise CustodyConflict(
                            f"包装单元 {unit_code} 在班次 {span.holder_code} 上，"
                            f"不能从班次 {leg} 卸下",
                            detail={"unit_code": unit_code, "event_id": event.event_id},
                        )
                    spans[-1] = _close(span, event)
                    open_span(HolderKind.HUB, hub, event, gap=False)
                elif span.holder_kind == HolderKind.HUB:
                    continue  # 已在枢纽，重复卸车忽略
                else:
                    raise CustodyConflict(
                        f"包装单元 {unit_code} 已签收，不能再卸车",
                        detail={"unit_code": unit_code, "event_id": event.event_id},
                    )
            else:
                open_span(HolderKind.HUB, hub, event, gap=True)
            continue

        if et == EventType.HANDOVER:
            to_kind = HolderKind(event.payload["to_holder_kind"])
            to_code = str(event.payload["to_holder_code"])
            if (
                span is not None
                and span.is_open
                and span.holder_kind == to_kind
                and span.holder_code == to_code
            ):
                continue  # 重复交接
            if span is not None and span.is_open:
                spans[-1] = _close(span, event)
                open_span(to_kind, to_code, event, gap=False)
            else:
                open_span(to_kind, to_code, event, gap=True)
            continue

        raise ValidationError(f"不支持的事件类型 {et}")

    return CustodyChain(unit_code, spans, orphans, rejected)
