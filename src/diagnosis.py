"""流程变体诊断：等待分布、滞留环节、逾期三分类与证据追溯。

两个时间视角（对应资料口径中的两类分钟）：
- arrived 视角：记录可用于值班判断的分钟，反映“当时实际知道什么”，
  是堵点回放与历史归因的主口径；补录在补录分钟之前不参与判断。
- 时钟视角：用动作的 earliest/latest 区间重建业务先后，仅在区间不重叠时
  可作时长归因；区间重叠意味着来源各自有时钟、可行顺序不唯一。

口径约束：
- 等待一律基于有效事件（已剔除 copy、已归并补录）。
- 并行确认（parallel）互不规定顺序，各自以共同前置计算等待；
  观察到的先后与命名顺序相反记 parallel_reversed 变体，仍属同一并行变体类。
- 前一动作 latest < 后一动作 earliest 才能把等待归因到单一环节；否则记 ambiguous，
  不把跨环节数字拼到某一环节，也不允许为同一实例在各环节分别挑最短数字。
- 逾期按每位学生自己的 eligible/due 核查；group 去重只在场次效率统计里做。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum

from .loader import Bundle
from .model import EffectiveEvent, Timeline, build_all_timelines

OBSERVATION_CUTOFF = 15292  # 全量资料（含 pilot_log）最后一条 arrived


class Attribution(str, Enum):
    STAGE = "stage"            # 区间不重叠，可归因到该环节
    AMBIGUOUS = "ambiguous"    # 区间重叠或补录乱序，可行顺序不唯一
    NA = "na"                  # 前置/本动作缺失，无法计算


class Outcome(str, Enum):
    COMPLETED = "completed"                  # 观察期内全部必备动作完成且未退回
    RETURNED = "returned"                    # 已明确退回
    NO_RECEIPT = "no_receipt"                # 到观察期仍无有效回执（未退回）
    IN_FLIGHT = "in_flight"                  # 已过交件期、流程未走完（非收收件缺失）
    NOT_YET_DUE = "not_yet_due"              # 尚未到交件时间


class Variant(str, Enum):
    NORMAL = "normal"
    TEACHER_ROUTE = "teacher_route"
    PARALLEL_REVERSED = "parallel_reversed"
    REGISTER_BYPASS = "register_bypass"
    BACKFILL_CORRECTED = "backfill_corrected"
    RETURN_CASE = "return_case"


@dataclass
class SegmentWait:
    stage: str
    predecessor: str
    waited_arrived: int | None       # 主口径：arrived 差
    waited_clock: int | None         # 时钟重建：earliest 差（仅供归因对照）
    attribution: Attribution
    clock_disjoint: bool | None
    backfill_affected: bool
    evidence: list[str] = field(default_factory=list)


@dataclass
class PlacementDiagnosis:
    placement_id: str
    site: str
    version: str
    group: str
    teacher: str
    state: str
    source: str                      # log（历史）或 pilot_log（试行观察）
    eligible_min: int
    due_min: int
    required_actions: tuple[str, ...]
    variant_tags: tuple[str, ...]
    outcome: Outcome
    current_stage: str
    parallel_overlap: bool           # 并行确认时钟区间是否重叠（顺序不可唯一确定）
    segments: list[SegmentWait]
    total_waited_arrived: int | None
    total_waited_attributable: int | None
    overdue_min: int | None
    evidence: list[str] = field(default_factory=list)
    retention_gaps: list[dict] = field(default_factory=list)
    copies: list[str] = field(default_factory=list)


def _clock_disjoint(prev: EffectiveEvent, nxt: EffectiveEvent) -> bool:
    return prev.latest < nxt.earliest


def compute_segments(timeline: Timeline) -> list[SegmentWait]:
    events = timeline.events
    required = timeline.required_actions
    parallel = set(timeline.parallel_pair)
    segments: list[SegmentWait] = []

    predecessor_of: dict[str, str] = {}
    for index, action in enumerate(required):
        if index == 0:
            continue
        if action == "scope_confirm" and action in parallel:
            predecessor_of[action] = "site_received"  # 并行确认的共同前置
        else:
            predecessor_of[action] = required[index - 1]

    for action in required[1:]:
        predecessor = predecessor_of[action]
        event = events.get(action)
        prev = events.get(predecessor)
        evidence = [event.record_id] if event else []
        if prev:
            evidence.append(prev.record_id)
        backfill_hit = bool(event and event.backfill) or bool(prev and prev.backfill)

        if event is None or event.is_returned or prev is None or prev.is_returned:
            segments.append(SegmentWait(
                stage=action, predecessor=predecessor,
                waited_arrived=None, waited_clock=None,
                attribution=Attribution.NA, clock_disjoint=None,
                backfill_affected=backfill_hit, evidence=evidence,
            ))
            continue

        waited_arrived = event.arrived - prev.arrived
        waited_clock = event.earliest - prev.earliest
        disjoint = _clock_disjoint(prev, event)

        # 补录造成 arrived 乱序（后动作先“可知”或时钟倒置）时不能作单一环节归因。
        order_break = waited_arrived < 0 or event.earliest < prev.earliest
        if disjoint and not order_break:
            attribution = Attribution.STAGE
        else:
            attribution = Attribution.AMBIGUOUS
            if not disjoint:
                evidence.append(
                    f"区间重叠 {predecessor}[{prev.earliest},{prev.latest}] "
                    f"{action}[{event.earliest},{event.latest}]"
                )
            if order_break:
                evidence.append("补录/时钟导致先后关系不成立，不作时长归因")

        segments.append(SegmentWait(
            stage=action, predecessor=predecessor,
            waited_arrived=waited_arrived, waited_clock=waited_clock,
            attribution=attribution, clock_disjoint=disjoint,
            backfill_affected=backfill_hit, evidence=evidence,
        ))
    return segments


def classify_variant(timeline: Timeline) -> tuple[Variant, ...]:
    tags: list[Variant] = []
    events = timeline.events
    required = list(timeline.required_actions)
    parallel = set(timeline.parallel_pair)

    if any(event.is_returned for event in events.values()):
        tags.append(Variant.RETURN_CASE)
    if any(event.backfill for event in events.values()):
        tags.append(Variant.BACKFILL_CORRECTED)

    site_event = events.get("site_received")
    first_event = events.get("first_confirm")
    if site_event is None and first_event is not None and not first_event.is_returned:
        tags.append(Variant.TEACHER_ROUTE)

    if {"first_confirm", "scope_confirm"} <= parallel:
        first_ev = events.get("first_confirm")
        scope_ev = events.get("scope_confirm")
        if first_ev and scope_ev and not first_ev.is_returned and not scope_ev.is_returned:
            if scope_ev.arrived < first_ev.arrived or scope_ev.earliest < first_ev.earliest:
                tags.append(Variant.PARALLEL_REVERSED)

    registered = events.get("registered")
    if registered and not registered.is_returned:
        confirmations = [a for a in required if a.endswith("_confirm")]
        if any((ev := events.get(a)) is None or ev.is_returned for a in confirmations):
            tags.append(Variant.REGISTER_BYPASS)

    if not tags:
        tags.append(Variant.NORMAL)
    return tuple(tags)


def classify_outcome(timeline: Timeline, cutoff: int) -> Outcome:
    placement = timeline.placement
    events = timeline.events

    if any(event.is_returned for event in events.values()):
        return Outcome.RETURNED
    if cutoff < placement.due_min:
        return Outcome.NOT_YET_DUE

    missing = [a for a in timeline.required_actions
               if a not in events or events[a].is_returned]
    if not missing:
        return Outcome.COMPLETED
    return Outcome.NO_RECEIPT if timeline.current_stage() == "site_received" \
        else Outcome.IN_FLIGHT


def retention_observations(timeline: Timeline, bundle: Bundle) -> list[dict]:
    placement_id = timeline.placement.id
    gaps: list[dict] = []
    for obs in bundle.observability:
        if obs.placement != placement_id:
            continue
        event = timeline.events.get(obs.action)
        if event is None:
            gaps.append({
                "action": obs.action, "window": [obs.from_min, obs.to_min],
                "source_state": obs.state, "resolved_by_record": None,
                "agreement": "窗口内确无记录（与留存缺口声明一致）",
            })
        else:
            within = obs.from_min <= event.earliest and event.latest <= obs.to_min
            gaps.append({
                "action": obs.action, "window": [obs.from_min, obs.to_min],
                "source_state": obs.state,
                "resolved_by_record": event.record_id,
                "arrived": event.arrived,
                "agreement": "记录在窗口结束后才到达（留存窗口缺口，非最终缺件）"
                if event.arrived > obs.to_min
                else ("声明窗口覆盖该记录时钟，来源声明与日志矛盾"
                      if within else "来源声明与日志部分不符"),
            })
    return gaps


def diagnose(bundle: Bundle, cutoff: int = OBSERVATION_CUTOFF,
             timelines: dict[str, Timeline] | None = None,
             source: str = "log") -> dict[str, PlacementDiagnosis]:
    timelines = timelines or build_all_timelines(bundle)
    result: dict[str, PlacementDiagnosis] = {}

    for pid, timeline in sorted(timelines.items()):
        placement = timeline.placement
        segments = compute_segments(timeline)
        variant_tags = classify_variant(timeline)
        outcome = classify_outcome(timeline, cutoff)

        # 并行确认之间的时钟区间是否重叠：重叠则两者的可行顺序不唯一，
        # 它们之间的相对先后不能作时长归因。
        first_ev = timeline.events.get("first_confirm")
        scope_ev = timeline.events.get("scope_confirm")
        parallel_overlap = bool(
            first_ev and scope_ev
            and not first_ev.is_returned and not scope_ev.is_returned
            and first_ev.earliest <= scope_ev.latest
            and scope_ev.earliest <= first_ev.latest
        )

        valid_waits = [seg.waited_arrived for seg in segments
                       if seg.waited_arrived is not None and seg.waited_arrived >= 0]
        total_waited = sum(valid_waits) if valid_waits else None
        attributable = sum(
            seg.waited_arrived for seg in segments
            if seg.attribution is Attribution.STAGE and seg.waited_arrived is not None
        )
        if not any(seg.attribution is Attribution.STAGE for seg in segments):
            attributable = None

        last_completed = None
        for action in timeline.required_actions:
            event = timeline.events.get(action)
            if event is not None and not event.is_returned:
                last_completed = event.arrived
        if outcome is Outcome.COMPLETED:
            # 完成实例：实际办结相对本人 due 的偏差（数据中均在 due 前办结，故为 0）。
            overdue_min = max(0, last_completed - placement.due_min)
        elif outcome is Outcome.NOT_YET_DUE:
            overdue_min = None
        elif outcome is Outcome.RETURNED:
            # 退回实例：以退回动作的到达时点计量“退回发生时已逾期多久”，
            # 不再用观察终点放大，避免把后续等待算到退回件头上。
            returned_event = next(
                event for event in timeline.events.values() if event.is_returned
            )
            overdue_min = max(0, returned_event.arrived - placement.due_min)
        else:
            # 在途/无回执：到观察终点为止，相对本人 due 的持续逾期。
            overdue_min = max(0, cutoff - placement.due_min)

        result[pid] = PlacementDiagnosis(
            placement_id=pid, site=placement.site, version=placement.version,
            group=placement.group, teacher=placement.teacher, state=placement.state,
            source=source, eligible_min=placement.eligible_min,
            due_min=placement.due_min, required_actions=timeline.required_actions,
            variant_tags=tuple(tag.value for tag in variant_tags),
            outcome=outcome, current_stage=timeline.current_stage(),
            parallel_overlap=parallel_overlap,
            segments=segments, total_waited_arrived=total_waited,
            total_waited_attributable=attributable, overdue_min=overdue_min,
            evidence=[event.id for event in timeline.raw_records],
            retention_gaps=retention_observations(timeline, bundle),
            copies=[event.id for event in timeline.copies],
        )
    return result


def diagnosis_to_dict(diagnoses: dict[str, PlacementDiagnosis]) -> list[dict]:
    rows = []
    for diagnosis in diagnoses.values():
        row = asdict(diagnosis)
        row["outcome"] = diagnosis.outcome.value
        row["segments"] = [
            {
                **{key: value for key, value in asdict(seg).items()
                   if key != "attribution"},
                "attribution": seg.attribution.value,
            }
            for seg in diagnosis.segments
        ]
        rows.append(row)
    return rows
