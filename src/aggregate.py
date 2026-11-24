"""聚合：等待分布、滞留环节、责任范围、场次效率（group 去重）。"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median

from .diagnosis import Attribution, Outcome, PlacementDiagnosis
from .loader import Bundle
from .model import Timeline


def wait_distribution(diagnoses: dict[str, PlacementDiagnosis]) -> dict:
    """按环节汇总等待分布，区分可归因与归因不清。

    补录影响的环节不进入常规分布（其等待由“补录解释变化”单独说明），
    避免把补录造成的跨期伪等待混入正常环节的分布数字。
    """
    per_stage: dict[str, dict] = defaultdict(lambda: {
        "attributable_waits": [], "ambiguous_waits": [], "na": 0,
        "backfill_affected": 0,
    })
    for diagnosis in diagnoses.values():
        for segment in diagnosis.segments:
            bucket = per_stage[segment.stage]
            if segment.backfill_affected:
                bucket["backfill_affected"] += 1
                continue
            if segment.attribution is Attribution.STAGE:
                bucket["attributable_waits"].append(segment.waited_arrived)
            elif segment.attribution is Attribution.AMBIGUOUS:
                if segment.waited_arrived is not None and segment.waited_arrived >= 0:
                    bucket["ambiguous_waits"].append(segment.waited_arrived)
            else:
                bucket["na"] += 1

    summary = {}
    for stage, bucket in sorted(per_stage.items()):
        waits = bucket["attributable_waits"]
        ambiguous = bucket["ambiguous_waits"]
        summary[stage] = {
            "可归因样本": len(waits),
            "可归因等待_分钟": {
                "最小": min(waits) if waits else None,
                "中位": median(waits) if waits else None,
                "最大": max(waits) if waits else None,
                "均值": round(sum(waits) / len(waits), 1) if waits else None,
            },
            "归因不清样本": len(ambiguous),
            "归因不清等待_分钟": {
                "中位": median(ambiguous) if ambiguous else None,
                "最大": max(ambiguous) if ambiguous else None,
            },
            "缺失无法计算": bucket["na"],
            "含补录环节数": bucket["backfill_affected"],
        }
    return summary


def stage_backlog(diagnoses: dict[str, PlacementDiagnosis]) -> dict:
    """滞留环节分布与仍可能的责任范围。"""
    unfinished = [d for d in diagnoses.values() if d.outcome not in
                  (Outcome.COMPLETED, Outcome.NOT_YET_DUE)]
    stage_counter = Counter(d.current_stage for d in unfinished)
    responsibility: dict[str, set[str]] = defaultdict(set)
    for d in unfinished:
        if d.current_stage == "site_received":
            responsibility[d.current_stage].add(f"site:{d.site}")
        elif d.current_stage == "registered":
            responsibility[d.current_stage].add("office")
        else:
            responsibility[d.current_stage].add(f"teacher:{d.teacher}")
    return {
        "未完成实例数": len(unfinished),
        "按环节": dict(sorted(stage_counter.items())),
        "责任范围": {stage: sorted(persons)
                  for stage, persons in sorted(responsibility.items())},
    }


def outcome_breakdown(diagnoses: dict[str, PlacementDiagnosis]) -> dict:
    """逾期三分类（明确分开，不混合）。"""
    counter = Counter(d.outcome.value for d in diagnoses.values())
    members: dict[str, list[str]] = defaultdict(list)
    overdue: dict[str, list[int]] = defaultdict(list)
    for d in diagnoses.values():
        members[d.outcome.value].append(d.placement_id)
        if d.overdue_min is not None:
            overdue[d.outcome.value].append(d.overdue_min)
    result = {}
    for key in ("completed", "returned", "no_receipt", "in_flight", "not_yet_due"):
        values = overdue.get(key, [])
        result[key] = {
            "数量": counter.get(key, 0),
            "实例": sorted(members.get(key, [])),
            "逾期分钟_中位": median(values) if values else None,
            "逾期分钟_最大": max(values) if values else None,
        }
    return result


def group_efficiency(bundle: Bundle, timelines: dict[str, Timeline]) -> dict:
    """场次效率：同一 group 的共用动作在一场工时里只计一次。

    每位学生的逾期仍按本人 eligible/due 单独核查（不在此处合并）。
    """
    group_actions: dict[str, set[str]] = defaultdict(set)
    group_members: dict[str, list[str]] = defaultdict(list)
    group_work_minutes: dict[str, int] = defaultdict(int)

    for pid, timeline in timelines.items():
        group = timeline.placement.group
        group_members[group].append(pid)
        for action, event in timeline.events.items():
            if event.is_returned:
                continue
            if action not in group_actions[group]:  # 共用动作只计一次
                group_actions[group].add(action)
                group_work_minutes[group] += max(0, event.latest - event.earliest)

    rows = []
    for group in sorted(group_members):
        members = group_members[group]
        rows.append({
            "group": group,
            "学生数": len(members),
            "去重后动作数": len(group_actions[group]),
            "原始动作记录数": sum(
                len([e for e in timelines[p].events.values() if not e.is_returned])
                for p in members
            ),
            "场次工时_分钟": group_work_minutes[group],
            "成员": sorted(members),
        })

    shared_groups = [row for row in rows if row["学生数"] > 1]
    return {
        "场次数": len(rows),
        "多名学生共用场次数": len(shared_groups),
        "去重节省动作人次": sum(
            row["原始动作记录数"] - row["去重后动作数"] for row in shared_groups
        ),
        "明细": rows,
    }


def variant_summary(diagnoses: dict[str, PlacementDiagnosis]) -> dict:
    counter = Counter(tag for d in diagnoses.values() for tag in d.variant_tags)
    members: dict[str, list[str]] = defaultdict(list)
    for d in diagnoses.values():
        for tag in d.variant_tags:
            members[tag].append(d.placement_id)
    return {
        tag: {"数量": counter[tag], "实例": sorted(members[tag])}
        for tag in sorted(counter)
    }


def version_diagnosis(bundle: Bundle,
                      diagnoses: dict[str, PlacementDiagnosis]) -> dict:
    """对照每个版本的正常变体和例外分别汇总。"""
    result: dict[str, dict] = {}
    by_version: dict[str, list[PlacementDiagnosis]] = defaultdict(list)
    for d in diagnoses.values():
        by_version[d.version].append(d)
    for version, items in sorted(by_version.items()):
        workflow = bundle.workflows[version]
        normal = [d for d in items if d.variant_tags == ("normal",)]
        result[version] = {
            "生效分钟": workflow.from_min,
            "scope_required_站点": list(workflow.scope_required),
            "接手口径": list(workflow.either),
            "实例数": len(items),
            "正常变体": len(normal),
            "例外实例": {
                tag: sorted(d.placement_id for d in items if tag in d.variant_tags)
                for tag in ("teacher_route", "parallel_reversed",
                            "register_bypass", "backfill_corrected", "return_case")
                if any(tag in d.variant_tags for d in items)
            },
        }
    return result
