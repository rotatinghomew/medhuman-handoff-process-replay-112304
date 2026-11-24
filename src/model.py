"""流程口径与逐实例时间线。

关键约定：
- 同一业务动作若存在补录（supersedes 指向原记录），以补录为准，原记录只保留为可追溯证据。
- kind=copy 的回执只证明“副本到达”，不构成接手，不进入义务完成判断。
- kind=returned 表示本次交件被退回，该动作不计为完成，实例进入“已明确退回”类。
- 并行动作（workflow.parallel）互不规定先后；只有前后动作的可能区间不重叠时，
  中间的等待才可归因到单一环节，否则记为归因不清。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .loader import Bundle, LogEvent, Placement

ACTIONS = (
    "student_send",
    "site_received",
    "first_confirm",
    "scope_confirm",
    "registered",
)


@dataclass
class EffectiveEvent:
    """归并补录、剔除副本后用于判断的事件。"""

    action: str
    actor: str
    earliest: int
    latest: int
    arrived: int
    kind: str
    record_id: str
    superseded: tuple[str, ...] = ()
    backfill: bool = False

    @property
    def is_returned(self) -> bool:
        return self.kind == "returned"


@dataclass
class Timeline:
    placement: Placement
    events: dict[str, EffectiveEvent]
    copies: list[LogEvent] = field(default_factory=list)
    raw_records: list[LogEvent] = field(default_factory=list)
    missing_actions: tuple[str, ...] = ()
    returned_actions: tuple[str, ...] = ()
    required_actions: tuple[str, ...] = ()
    parallel_pair: tuple[str, ...] = ()
    handoff_route: str = ""
    eligible_actors: tuple[str, ...] = ()

    def completed_actions(self) -> tuple[str, ...]:
        return tuple(
            action for action in self.required_actions
            if action in self.events and not self.events[action].is_returned
        )

    def current_stage(self) -> str:
        """实例当前滞留的环节：第一个未完成的必备动作。"""
        for action in self.required_actions:
            event = self.events.get(action)
            if event is None or event.is_returned:
                return action
        return "done"

    def effective_arrived(self, action: str) -> int | None:
        event = self.events.get(action)
        return None if event is None else event.arrived

    def effective_earliest(self, action: str) -> int | None:
        event = self.events.get(action)
        return None if event is None else event.earliest


def required_actions_for(placement: Placement, bundle: Bundle) -> tuple[str, ...]:
    """按实例适用版本的公示，给出该实例必备的动作序列。"""
    workflow = bundle.workflows[placement.version]
    required = ["student_send", "site_received", "first_confirm"]
    if placement.site in workflow.scope_required:
        required.append("scope_confirm")
    required.append("registered")
    return tuple(required)


def handoff_route_for(placement: Placement, bundle: Bundle) -> str:
    """either 口径：site 表示教学点可代收，teacher 表示必须等有资格的老师接手。"""
    workflow = bundle.workflows[placement.version]
    options = workflow.either
    if options == ("site", "teacher"):
        # V1：教学点与老师均可接手；以实际带教归属记录判定，缺省走 site 口径。
        return "site"
    return options[0]


def build_timeline(placement: Placement, bundle: Bundle,
                   event_source: list[LogEvent] | None = None) -> Timeline:
    """把原始日志归并为一个实例的有效时间线。"""
    records = [event for event in (event_source or bundle.log)
               if event.placement == placement.id]
    workflow = bundle.workflows[placement.version]
    required = required_actions_for(placement, bundle)

    copies = [event for event in records if event.is_copy]
    substantive = [event for event in records if not event.is_copy]

    # 补录归并：被指向的原记录降级为证据，补录成为该动作的有效记录。
    superseded_ids = {event.supersedes for event in substantive if event.is_backfill}
    events: dict[str, EffectiveEvent] = {}
    backfill_chains: dict[str, list[str]] = {}
    for event in sorted(substantive, key=lambda item: (item.arrived, item.id)):
        if event.id in superseded_ids:
            continue
        replaced = tuple(
            old.id for old in substantive if old.id == event.supersedes
        )
        if replaced:
            backfill_chains[event.action] = list(replaced)
        events[event.action] = EffectiveEvent(
            event.action, event.actor, event.earliest, event.latest,
            event.arrived, event.kind, event.id, replaced,
            backfill=bool(replaced),
        )

    missing = tuple(action for action in required if action not in events)
    returned = tuple(
        action for action, event in events.items() if event.is_returned
    )

    return Timeline(
        placement=placement,
        events=events,
        copies=copies,
        raw_records=records,
        missing_actions=missing,
        returned_actions=returned,
        required_actions=required,
        parallel_pair=workflow.parallel,
        handoff_route=handoff_route_for(placement, bundle),
    )


def build_all_timelines(bundle: Bundle,
                        event_source: list[LogEvent] | None = None
                        ) -> dict[str, Timeline]:
    return {
        pid: build_timeline(placement, bundle, event_source)
        for pid, placement in bundle.placements.items()
    }
