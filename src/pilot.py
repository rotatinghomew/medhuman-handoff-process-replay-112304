"""试行改版验效：基线与试行规则回放对比、积压转移、观察与推演分离。"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from statistics import median

from .loader import Bundle
from .model import build_all_timelines, build_timeline
from .replay import (
    ReplayResult,
    pilot_eligibility,
    replay_rule,
)
from .diagnosis import OBSERVATION_CUTOFF, diagnose


def _summarize(results: list[ReplayResult]) -> dict:
    completable = [r for r in results if not r.blocked_reason and r.eligible]
    blocked = [r for r in results if r.blocked_reason]
    queues = [r.total_queue_min for r in completable if r.total_queue_min is not None]
    return {
        "实例数": len(results),
        "可完成回放": len(completable),
        "阻塞数": len(blocked),
        "阻塞类型": dict(Counter(r.blocked_kind or "not_eligible" for r in blocked)),
        "总排队分钟": sum(queues),
        "人均排队_中位": median(queues) if queues else None,
        "人均排队_最大": max(queues) if queues else None,
    }


def _stage_queue(results: list[ReplayResult]) -> dict[str, int]:
    counter: dict[str, int] = defaultdict(int)
    for result in results:
        for event in result.events:
            counter[event.action] += max(0, event.queued_min or 0)
    return dict(sorted(counter.items()))


def compare_rules(bundle: Bundle) -> dict:
    """在同一批历史工作量上回放 baseline 与 pilot，输出差异与积压转移。

    两个范围分别回答不同问题：
    - counterfactual（反事实）：把试行规则套到全部历史工作量，检验规则本身的效果上限；
    - constrained（受约束）：只在 locked/announced 允许的在途范围启用，
      即“旧单还能按原公示办结、新单有人接”的真实可启用范围。
    """
    ids = sorted(bundle.placements)
    # 系统视角：全部实例共用容量账本，反映真实窗口竞争与溢出。
    baseline_shared = {r.placement_id: r for r in replay_rule(bundle, "baseline", ids)}
    pilot_shared = {r.placement_id: r for r in replay_rule(bundle, "pilot", ids)}
    # 配对视角：同实例两规则各用独立账本，差异只来自规则本身而非他人占用。
    baseline_iso = {r.placement_id: r for r in
                    replay_rule(bundle, "baseline", ids, shared_book=False)}
    pilot_iso = {r.placement_id: r for r in
                 replay_rule(bundle, "pilot", ids, shared_book=False)}

    eligible_ids = [pid for pid, result in pilot_shared.items() if result.eligible]
    ineligible = [
        {"placement": pid, "site": bundle.placements[pid].site,
         "原因": pilot_shared[pid].blocked_reason}
        for pid in ids if not pilot_shared[pid].eligible
    ]

    base_eligible = [baseline_shared[pid] for pid in eligible_ids]
    pilot_eligible = [pilot_shared[pid] for pid in eligible_ids]

    # 隔离配对：只能在两条规则都能完整回放的实例上做差异，禁止跨实例拼接。
    paired = [
        pid for pid in eligible_ids
        if not baseline_iso[pid].blocked_reason and not pilot_iso[pid].blocked_reason
    ]
    diffs = []
    for pid in paired:
        base, pilot_result = baseline_iso[pid], pilot_iso[pid]
        diffs.append({
            "placement": pid,
            "site": base.site,
            "基线排队": base.total_queue_min,
            "试行排队": pilot_result.total_queue_min,
            "排队缩短": base.total_queue_min - pilot_result.total_queue_min,
            "基线完成分钟": base.completion_min,
            "试行完成分钟": pilot_result.completion_min,
        })

    shortened = [d for d in diffs if d["排队缩短"] > 0]

    # 反事实视角：所有历史 site 路线实例若改走 teacher 路线（忽略启用约束）。
    counterfactual_ids = [
        pid for pid in ids
        if "site_received" in build_timeline(
            bundle.placements[pid], bundle
        ).required_actions
    ]
    cf_base = replay_rule(bundle, "baseline", counterfactual_ids)
    cf_pilot = replay_rule(bundle, "pilot", counterfactual_ids)

    return {
        "回放纪律": {
            "系统视角": "全部实例共用容量账本，含跨站点窗口竞争与溢出",
            "配对视角": "同实例两规则使用独立容量账本，差异仅来自规则本身",
        },
        "试行适用范围": {
            "合格实例": eligible_ids,
            "合格数": len(eligible_ids),
            "不宜启用实例": ineligible,
        },
        "受约束视角_仅可启用范围": {
            "系统容量_基线": _summarize(base_eligible),
            "系统容量_试行": _summarize(pilot_eligible),
            "系统各环节排队_基线": _stage_queue(base_eligible),
            "系统各环节排队_试行": _stage_queue(pilot_eligible),
            "隔离配对差异": diffs,
            "可缩短等待_隔离口径": {
                "实例数": len(shortened),
                "总缩短分钟": sum(d["排队缩短"] for d in shortened),
                "中位缩短": median([d["排队缩短"] for d in shortened]) if shortened else 0,
            },
            "基线溢出积压": [
                {"placement": r.placement_id, "环节": r.blocked_reason}
                for r in base_eligible if r.blocked_kind == "window_exhausted"
            ],
            "试行仍阻塞": [
                {"placement": r.placement_id, "原因": r.blocked_reason,
                 "类型": r.blocked_kind}
                for r in pilot_eligible if r.blocked_reason
            ],
        },
        "反事实视角_全历史工作量": {
            "覆盖实例数": len(counterfactual_ids),
            "基线汇总": _summarize(cf_base),
            "试行汇总": _summarize(cf_pilot),
            "各环节排队_基线": _stage_queue(cf_base),
            "各环节排队_试行": _stage_queue(cf_pilot),
            "说明": "含未 announced/locked 站点，仅用于估计规则本身的效果上限，不构成启用建议",
        },
    }


def observed_pilot(bundle: Bundle, cutoff: int = OBSERVATION_CUTOFF) -> dict:
    """pilot_log 中实际观察到的试行结果，与历史推演严格分开。"""
    pilot_ids = sorted({event.placement for event in bundle.pilot_log})
    timelines = {
        pid: build_timeline(bundle.placements[pid], bundle, bundle.pilot_log)
        for pid in pilot_ids
    }
    diagnoses = diagnose(bundle, cutoff, timelines, source="pilot_log")
    rows = []
    for pid in pilot_ids:
        diagnosis = diagnoses[pid]
        rows.append({
            "placement": pid,
            "site": diagnosis.site,
            "结果": diagnosis.outcome.value,
            "滞留环节": diagnosis.current_stage,
            "逾期分钟": diagnosis.overdue_min,
            "有效记录": [event.record_id for event in timelines[pid].events.values()],
            "证据": diagnosis.evidence,
        })
    return {"观察终点分钟": cutoff, "实例": rows}


def backfill_explanation_shift(bundle: Bundle) -> list[dict]:
    """补录改变堵点解释时，区分是改版估计变化还是执行偏差。

    判定：补录事件的时钟动作时间在原流程顺序内，但 arrived 晚于后续动作，
    说明当时值班判断缺少该事实——属于记录/执行时点偏差，不是规则改版的估计变化。
    """
    timelines = build_all_timelines(bundle)
    shifts = []
    for pid, timeline in sorted(timelines.items()):
        for action, event in timeline.events.items():
            if not event.backfill:
                continue
            original = next(
                (record for record in timeline.raw_records
                 if record.id == event.superseded[0]), None
            )
            later_actions = [
                later.action for later in timeline.events.values()
                if later.arrived < event.arrived and not later.backfill
            ]
            shifts.append({
                "placement": pid,
                "动作": action,
                "原记录": event.superseded[0],
                "补录记录": event.record_id,
                "原记录到达": original.arrived if original else None,
                "补录到达": event.arrived,
                "动作时钟区间": [event.earliest, event.latest],
                "补录前已被当成完成的后续动作": later_actions,
                "解释变化类型": "执行/留存偏差（当时事实未到达，非规则改版估计变化）",
                "对堵点判断的影响": (
                    "旧口径从最后回执倒推会把该步当成早已完成；"
                    "按 arrived 重算后，等待应归到补录到达之前的缺口，而非后续环节"
                ),
            })
    return shifts
