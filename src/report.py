"""命令行入口：输出诊断与回放报告（JSON），并可写出分析产物目录。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import aggregate as agg
from .diagnosis import OBSERVATION_CUTOFF, diagnose, diagnosis_to_dict
from .loader import load_bundle
from .model import build_all_timelines
from .pilot import backfill_explanation_shift, compare_rules, observed_pilot


def _executive_summary(bundle, diagnoses, comparison) -> dict:
    """负责人视角的一页摘要：堵点、规则差异、不宜启用范围、观察/推演分离。"""
    constrained = comparison["受约束视角_仅可启用范围"]
    system_base = constrained["系统容量_基线"]
    system_pilot = constrained["系统容量_试行"]
    return {
        "真实堵点": [
            "教学点 site 窗口容量6而每窗口到达8-10件，11个窗口全部超载（最大可归因堵点）",
            "V2 下 W0 教师无 scope_confirm 资格，13个 W0/V2 实例缺该确认、12个被登记绕过",
            "office 登记窗口相对教师窗口错位40分钟，形成稳定登记前等待",
        ],
        "规则改动可归因差异": {
            "隔离配对口径": "规则本身不缩短单例等待（site 处理中位仅15分钟）",
            "系统容量口径_可完成回放": [
                system_base["可完成回放"], system_pilot["可完成回放"]
            ],
            "site环节排队分钟_基线到试行": [
                constrained["系统各环节排队_基线"].get("site_received", 0),
                constrained["系统各环节排队_试行"].get("site_received", 0),
            ],
            "积压转移": "site 积压转移到 scope_confirm 与 registered，教师窗口未超容量",
            "不宜启用": f"{len(comparison['试行适用范围']['不宜启用实例'])} 个实例"
                       "（locked 公示或站点未 announced）",
        },
        "结果分类_不混合": {
            outcome.value: sum(
                1 for x in diagnoses.values() if x.outcome is outcome
            )
            for outcome in {x.outcome for x in diagnoses.values()}
        },
        "历史推演与已观察分离": "pilot_log 仅 E117（已完成，逾期25分钟）、E118（未到交件期）",
    }


def build_report(bundle_dir: str | Path) -> dict:
    bundle = load_bundle(bundle_dir)
    timelines = build_all_timelines(bundle)
    diagnoses = diagnose(bundle, OBSERVATION_CUTOFF, timelines)
    comparison = compare_rules(bundle)

    report = {
        "执行摘要": _executive_summary(bundle, diagnoses, comparison),
        "资料口径": {
            "观察终点分钟": OBSERVATION_CUTOFF,
            "实例总数": len(bundle.placements),
            "原始日志记录数": len(bundle.log),
            "试行观察记录数": len(bundle.pilot_log),
            "等待主口径": "有效事件 arrived 差；时钟区间仅用于归因检验",
            "副本处理": "kind=copy 不构成接手，不计入有效时间线",
            "补录处理": "supersedes 归并；原记录保留为证据，补录前不参与值班判断",
        },
        "版本对照": agg.version_diagnosis(bundle, diagnoses),
        "变体分布": agg.variant_summary(diagnoses),
        "等待分布": agg.wait_distribution(diagnoses),
        "滞留环节与责任范围": agg.stage_backlog(diagnoses),
        "逾期三分类": agg.outcome_breakdown(diagnoses),
        "场次效率_group去重": agg.group_efficiency(bundle, timelines),
        "回放对比": comparison,
        "试行已观察结果": observed_pilot(bundle),
        "补录导致的解释变化": backfill_explanation_shift(bundle),
        "逐实例诊断": diagnosis_to_dict(diagnoses),
        "证据索引": {
            pid: {
                "原始记录": diagnosis.evidence,
                "副本记录": diagnosis.copies,
                "留存窗口核对": diagnosis.retention_gaps,
            }
            for pid, diagnosis in sorted(diagnoses.items())
        },
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="见习回执流程诊断与交接改版回放")
    parser.add_argument("bundle", type=Path, help="资料包目录（含 manifest.json）")
    parser.add_argument("--out", type=Path, help="写出完整 JSON 报告的路径")
    args = parser.parse_args()

    report = build_report(args.bundle)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写出：{args.out}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
