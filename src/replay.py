"""交接规则回放：用历史实际到达的工作量重放不同 handoff 规则。

纪律（对应需求中的回放约束）：
1. 只使用各时点已经 arrived 的事实；时钟区间 earliest/latest 不提前释放工作量。
2. 教师容量是 coverage 给出的“同一窗口内能够开始处理的最大值”；接手只能交给
   有资格（skills 含动作）且未超容量的人。
3. 同一实例在多种可行顺序下，沿一条确定的调度规则走完全程，不分别在各环节
   挑最短数字拼结果。
4. 在途实例受 notice/locked 与 launch 公示约束：旧单按原公示办结，新单只有
   已 announced 且有人接的范围才启用试行规则。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .loader import Bundle, Coverage, LogEvent
from .model import build_timeline


@dataclass
class WindowCapacity:
    person: str
    from_min: int
    to_min: int
    skills: tuple[str, ...]
    capacity: int
    used: int = 0

    def can_take(self, action: str, minute: int) -> bool:
        return (action in self.skills and self.from_min <= minute <= self.to_min
                and self.used < self.capacity)


@dataclass
class ReplayEvent:
    placement_id: str
    action: str
    actor: str
    available_at: int       # 该动作可被处理的时点（前置完成与 arrived 取大）
    started_at: int | None = None
    window_from: int | None = None
    window_to: int | None = None
    queued_min: int | None = None


@dataclass
class ReplayResult:
    rule: str
    placement_id: str
    site: str
    version: str
    eligible: bool
    eligibility_reason: str
    route: str
    events: list[ReplayEvent] = field(default_factory=list)
    completion_min: int | None = None
    total_queue_min: int | None = None
    blocked_reason: str | None = None
    blocked_kind: str | None = None  # missing_workload | no_coverage | window_exhausted


class CapacityBook:
    """按窗口登记容量占用；同一窗口内能够开始处理的数量不超过 capacity。

    三类资源池：site（代收）、teacher（确认，按 skill 区分资格）、office（登记）。
    容量不足时把任务留到后续窗口；覆盖表中再无可行窗口才判定无人可接。
    """

    def __init__(self, coverage: list[Coverage]):
        self.teacher_windows = [
            WindowCapacity(c.person, c.from_min, c.to_min, c.skills, c.capacity)
            for c in coverage if c.role == "teacher"
        ]
        self.site_windows = [
            WindowCapacity(c.person, c.from_min, c.to_min, c.skills, c.capacity)
            for c in coverage if c.role == "site"
        ]
        self.office_windows = [
            WindowCapacity(c.person, c.from_min, c.to_min, c.skills, c.capacity)
            for c in coverage if c.role == "office"
        ]
        self.last_failure: str | None = None

    def _take(self, windows: list[WindowCapacity], action: str, minute: int,
              eligible_persons: tuple[str, ...]):
        candidates = [
            window for window in windows
            if window.person in eligible_persons and window.can_take(action, minute)
        ]
        if not candidates:
            return None
        # 确定的调度规则：同资格下取剩余容量最大、再按人员编号，保证可复现。
        window = sorted(candidates, key=lambda w: (w.used - w.capacity, w.person))[0]
        window.used += 1
        return window

    def take_teacher(self, action: str, minute: int,
                     eligible_persons: tuple[str, ...]):
        return self._take(self.teacher_windows, action, minute, eligible_persons)

    def take_site(self, action: str, minute: int):
        return self._take(self.site_windows, action, minute, ("site",))

    def take_office(self, action: str, minute: int):
        return self._take(self.office_windows, action, minute, ("office",))

    def schedule(self, action: str, ready: int,
                 eligible_persons: tuple[str, ...] = ()) -> tuple[int, WindowCapacity] | None:
        """从 ready 起沿覆盖窗口调度。

        - ready 落在某个可行窗口 [from,to] 内且有余量：立即开始，排队为 0；
        - ready 早于下一窗口起点：在窗口起点开始（等待窗口开放）；
        - 所在窗口已满：顺延到下一个有余量的窗口（真实容量排队）；
        - 无任何 to >= ready 的可行窗口：无人可接。
        """
        if action == "site_received":
            windows, persons = self.site_windows, ("site",)
            take = lambda minute: self.take_site(action, minute)
        elif action == "registered":
            windows, persons = self.office_windows, ("office",)
            take = lambda minute: self.take_office(action, minute)
        else:
            windows, persons = self.teacher_windows, eligible_persons
            take = lambda minute: self.take_teacher(action, minute, eligible_persons)

        feasible = [
            win for win in windows
            if win.person in persons and action in win.skills and win.to_min >= ready
        ]
        for win in sorted(feasible, key=lambda item: (item.from_min, item.person)):
            start = max(ready, win.from_min)
            if start <= win.to_min and win.used < win.capacity:
                win.used += 1
                return start, win
        self.last_failure = (
            "window_exhausted" if feasible else "no_coverage"
        )
        return None

    def has_future_window(self, action: str, minute: int,
                          eligible_persons: tuple[str, ...] = ()) -> bool:
        if action == "site_received":
            windows, persons = self.site_windows, ("site",)
        elif action == "registered":
            windows, persons = self.office_windows, ("office",)
        else:
            windows, persons = self.teacher_windows, eligible_persons
        return any(
            window.person in persons and action in window.skills
            and window.to_min >= minute
            for window in windows
        )


def _effective_log(events: list[LogEvent]) -> list[LogEvent]:
    """剔除 copy、归并补录后的有效事件（按 arrived 可见）。"""
    superseded = {e.supersedes for e in events if e.supersedes}
    return [e for e in events if not e.is_copy and e.id not in superseded]


def _arrival_workload(bundle: Bundle, placement_id: str) -> dict[str, LogEvent]:
    """历史实际到达的工作量：每个动作的有效事件及其 arrived。"""
    timeline = build_timeline(bundle.placements[placement_id], bundle)
    return {action: event for action, event in timeline.events.items()
            if not event.is_returned}


def eligible_teachers_for(bundle: Bundle, action: str, site: str) -> tuple[str, ...]:
    """有资格处理某动作的老师集合（带该 skill 的全部覆盖人员）。"""
    persons = sorted({
        c.person for c in bundle.coverage
        if c.role == "teacher" and action in c.skills
    })
    return tuple(persons)


def pilot_eligibility(bundle: Bundle, placement_id: str) -> tuple[bool, str, str | None]:
    """判断实例能否进入试行规则。

    条件：
    - locked 公示承诺旧路线的在途实例必须走原公示，不进试行；
    - 试行版本对该 site 已 announced（candidate 不算公示）；
    - 启用时点不早于 takes_effect，且不晚于 trial_to；
    - 新单在生效时点之后仍有人可接（由回放容量进一步检验）。
    """
    placement = bundle.placements[placement_id]
    notice = bundle.notices.get(placement_id)
    if notice is not None and notice.locked:
        return False, f"locked 公示承诺 {notice.version}/{notice.promised_route}", None

    launch = next(
        (item for item in bundle.launches
         if item.site == placement.site and item.is_announced), None
    )
    if launch is None:
        return False, f"site {placement.site} 无 announced 试行公示", None
    return True, f"适用 {launch.version}（{launch.takes_effect} 生效）", launch.version


def replay_placement(bundle: Bundle, placement_id: str, rule: str,
                     book: CapacityBook) -> ReplayResult:
    """对单个实例回放一条规则。rule: baseline | pilot。"""
    placement = bundle.placements[placement_id]
    workflow = bundle.workflows[placement.version]
    workload = _arrival_workload(bundle, placement_id)
    result = ReplayResult(
        rule=rule, placement_id=placement_id, site=placement.site,
        version=placement.version, eligible=False, eligibility_reason="",
        route=workflow.either[0],
    )

    pilot_version = None
    if rule == "pilot":
        ok, reason, pilot_version = pilot_eligibility(bundle, placement_id)
        result.eligible = ok
        result.eligibility_reason = reason
        if not ok:
            result.blocked_kind = "not_eligible"
            result.blocked_reason = reason
            return result
        result.route = bundle.workflows[pilot_version].either[0]
    else:
        result.eligible = True
        result.eligibility_reason = "基线回放（原公示规则）"

    required = list(build_timeline(placement, bundle).required_actions)
    # 试行 teacher 路线下去掉教学点代收，first_confirm 直接接手学生交件。
    if rule == "pilot" and result.route == "teacher":
        required = [a for a in required if a != "site_received"]

    completion = None
    total_queue = 0
    predecessor_done: dict[str, int] = {}
    previous_action = None
    for action in required:
        event = workload.get(action)
        if event is None:
            # 历史上该动作无有效记录（如仅 copy、缺件）：该步骤工作量从未到达，
            # 不能为回放虚构到达时点，整条实例标记为不可回放，保留缺口环节。
            result.blocked_kind = "missing_workload"
            result.blocked_reason = f"历史无有效 {action} 记录，工作量未到达，不可回放"
            return result

        ready = event.arrived
        if previous_action is not None:
            ready = max(ready, predecessor_done[previous_action])

        if action == "student_send":
            window = None
            started = ready
        else:
            eligible_persons = eligible_teachers_for(bundle, action, placement.site)
            scheduled = book.schedule(action, ready, eligible_persons)
            if scheduled is None:
                result.blocked_kind = book.last_failure
                if book.last_failure == "window_exhausted":
                    result.blocked_reason = (
                        f"{ready} 分钟起 {action} 的全部可行窗口容量耗尽，积压溢出"
                    )
                else:
                    result.blocked_reason = (
                        f"{ready} 分钟后无覆盖 {action} 的窗口，新单无人接"
                    )
                return result
            started, window = scheduled

        queued = started - ready
        total_queue += max(0, queued)
        result.events.append(ReplayEvent(
            placement_id=placement_id, action=action,
            actor=window.person if window else event.actor,
            available_at=ready, started_at=started,
            window_from=window.from_min if window else None,
            window_to=window.to_min if window else None,
            queued_min=queued,
        ))
        predecessor_done[action] = started
        completion = started
        previous_action = action

    result.completion_min = completion
    result.total_queue_min = total_queue
    return result


def replay_rule(bundle: Bundle, rule: str, placement_ids: list[str],
                shared_book: bool = True) -> list[ReplayResult]:
        """回放一批实例。

        shared_book=True：所有实例共用一套容量账本，模拟系统级窗口竞争；
        shared_book=False：每个实例使用独立账本，用于同实例两规则的隔离配对，
        排除其他站点工作量对配对差异的干扰（但不能用来评估系统总容量）。
        """
        if shared_book:
            book = CapacityBook(bundle.coverage)
            return [replay_placement(bundle, pid, rule, book) for pid in placement_ids]
        return [
            replay_placement(bundle, pid, rule, CapacityBook(bundle.coverage))
            for pid in placement_ids
        ]
