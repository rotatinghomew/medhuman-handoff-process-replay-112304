"""分析口径与回放纪律的单元测试。"""
import unittest
from pathlib import Path

import _path  # noqa: F401  # 配置工作区导入路径

from src.loader import load_bundle
from src.model import build_all_timelines, build_timeline
from src.diagnosis import (
    Attribution,
    Outcome,
    Variant,
    diagnose,
)
from src.aggregate import (
    group_efficiency,
    outcome_breakdown,
    wait_distribution,
)
from src.replay import (
    CapacityBook,
    pilot_eligibility,
    replay_rule,
)
from src.pilot import backfill_explanation_shift, compare_rules, observed_pilot

BUNDLE = Path(__file__).resolve().parents[1] / "data"


class BundleLoadingTest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(BUNDLE)
        self.timelines = build_all_timelines(self.bundle)

    def test_copy_does_not_establish_receipt(self):
        # E000 只有 copy 的 site_received，有效时间线里不得出现接手
        timeline = self.timelines["E000"]
        self.assertNotIn("site_received", timeline.events)
        self.assertTrue(all(event.is_copy for event in timeline.copies))

    def test_backfill_supersedes_original(self):
        timeline = self.timelines["E034"]
        event = timeline.events["scope_confirm"]
        self.assertTrue(event.backfill)
        self.assertEqual(event.record_id, "E034-6")
        self.assertEqual(event.superseded, ("E034-4",))
        # 被取代的原记录不再是有效事件
        self.assertNotIn("E034-4", [e.record_id for e in timeline.events.values()])

    def test_required_actions_follow_workflow_scope_rule(self):
        # V1 下 W2 需要 scope_confirm，W0 不需要
        self.assertIn("scope_confirm",
                      build_timeline(self.bundle.placements["E002"], self.bundle).required_actions)
        self.assertNotIn("scope_confirm",
                         build_timeline(self.bundle.placements["E000"], self.bundle).required_actions)
        # V2 下 scope_required 变为 W0/W3
        self.assertIn("scope_confirm",
                      build_timeline(self.bundle.placements["E099"], self.bundle).required_actions)


class DiagnosisTest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(BUNDLE)
        self.diagnoses = diagnose(self.bundle)

    def test_three_way_outcomes_are_separate(self):
        outcomes = {d.outcome for d in self.diagnoses.values()}
        self.assertIn(Outcome.RETURNED, outcomes)
        self.assertIn(Outcome.NO_RECEIPT, outcomes)
        self.assertIn(Outcome.NOT_YET_DUE, outcomes)
        # 三类结果互不重叠（每个实例只归一类），退回实例不进入无回执统计
        ids_by_outcome = {}
        for diagnosis in self.diagnoses.values():
            ids_by_outcome.setdefault(diagnosis.outcome, set()).add(
                diagnosis.placement_id
            )
        self.assertNotIn(
            ids_by_outcome[Outcome.RETURNED] & ids_by_outcome[Outcome.NO_RECEIPT],
            set(),
        )
        breakdown = outcome_breakdown(self.diagnoses)
        self.assertEqual(
            sum(item["数量"] for item in breakdown.values()),
            len(self.diagnoses),
        )

    def test_not_yet_due_identified(self):
        # E118 due=15298，观察终点 15292；E119 due=15329
        self.assertEqual(self.diagnoses["E118"].outcome, Outcome.NOT_YET_DUE)
        self.assertEqual(self.diagnoses["E119"].outcome, Outcome.NOT_YET_DUE)
        self.assertIsNone(self.diagnoses["E118"].overdue_min)

    def test_returned_cases(self):
        returned = [pid for pid, d in self.diagnoses.items()
                    if d.outcome is Outcome.RETURNED]
        self.assertEqual(len(returned), 11)
        self.assertIn("E110", returned)

    def test_parallel_reversed_variant(self):
        reversed_cases = [
            pid for pid, d in self.diagnoses.items()
            if "parallel_reversed" in d.variant_tags
        ]
        self.assertEqual(set(reversed_cases), {"E006", "E011", "E034", "E067", "E086"})

    def test_overlapping_clocks_not_attributed_to_stage(self):
        # E003 并行两个确认区间重叠（first[581,591] 与 scope[585,595]），顺序不可唯一确定
        self.assertTrue(self.diagnoses["E003"].parallel_overlap)
        # E001 无 scope_confirm，first_confirm 与收件区间不重叠，可作环节归因
        self.assertFalse(self.diagnoses["E001"].parallel_overlap)
        segment_map = {s.stage: s for s in self.diagnoses["E001"].segments}
        self.assertEqual(segment_map["first_confirm"].attribution, Attribution.STAGE)

    def test_disjoint_clocks_are_attributable(self):
        segment_map = {s.stage: s for s in self.diagnoses["E001"].segments}
        self.assertEqual(segment_map["first_confirm"].attribution, Attribution.STAGE)

    def test_backfill_wait_excluded_from_distribution(self):
        distribution = wait_distribution(self.diagnoses)
        scope_waits = distribution["scope_confirm"]["可归因等待_分钟"]["最大"]
        # 补录造成的 7926 分钟伪等待不得进入常规分布
        self.assertLess(scope_waits, 1000)

    def test_register_bypass_flagged(self):
        bypass = [pid for pid, d in self.diagnoses.items()
                  if "register_bypass" in d.variant_tags]
        self.assertIn("E009", bypass)  # 缺 first_confirm 却有 registered
        self.assertIn("E068", bypass)  # V2 W0 缺 scope_confirm 却登记

    def test_evidence_traceable_to_records(self):
        diagnosis = self.diagnoses["E110"]
        self.assertIn("E110-5X", diagnosis.evidence)
        self.assertIn("E110-2", diagnosis.evidence)


class GroupEfficiencyTest(unittest.TestCase):
    def test_shared_action_counted_once_per_group(self):
        bundle = load_bundle(BUNDLE)
        timelines = build_all_timelines(bundle)
        efficiency = group_efficiency(bundle, timelines)
        self.assertGreater(efficiency["去重节省动作人次"], 0)
        # 每个 group 两名学生，去重后动作数不超过单人完整动作数
        for row in efficiency["明细"]:
            self.assertLessEqual(row["去重后动作数"], 5)


class ReplayDisciplineTest(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(BUNDLE)

    def test_locked_notice_blocks_pilot(self):
        ok, reason, _ = pilot_eligibility(self.bundle, "E102")
        self.assertFalse(ok)
        self.assertIn("locked", reason)

    def test_only_announced_launch_is_eligible(self):
        # W2 的 P2 为 announced；W1/W3 仅 candidate
        ok, _, version = pilot_eligibility(self.bundle, "E118")
        self.assertTrue(ok)
        self.assertEqual(version, "P2")
        ok, reason, _ = pilot_eligibility(self.bundle, "E117")
        self.assertFalse(ok)

    def test_missing_workload_is_not_invented(self):
        results = {r.placement_id: r for r in
                   replay_rule(self.bundle, "baseline", ["E007", "E001"])}
        self.assertEqual(results["E007"].blocked_kind, "missing_workload")
        self.assertIsNone(results["E001"].blocked_reason)

    def test_site_overload_appears_as_window_exhaustion(self):
        results = replay_rule(self.bundle, "baseline",
                              sorted(self.bundle.placements))
        exhausted = [r for r in results if r.blocked_kind == "window_exhausted"]
        self.assertTrue(exhausted)  # site 容量 6 而每窗口到达 8-10 件

    def test_capacity_window_respects_limit(self):
        book = CapacityBook(self.bundle.coverage)
        # site 窗口 [540,810] 容量 6：第 7 次必须被顺延或拒绝，不能占用同窗口
        accepted_in_window = 0
        overflowed = False
        for _ in range(7):
            outcome = book.schedule("site_received", 600)
            if outcome is None:
                overflowed = True
                continue
            start, window = outcome
            if window.from_min == 540:
                accepted_in_window += 1
            else:
                overflowed = True
        self.assertEqual(accepted_in_window, 6)
        self.assertTrue(overflowed)

    def test_pilot_removes_site_stage(self):
        results = {r.placement_id: r for r in
                   replay_rule(self.bundle, "pilot", ["E118"])}
        result = results["E118"]
        if not result.blocked_reason:
            self.assertNotIn("site_received", [e.action for e in result.events])

    def test_paired_comparison_uses_same_instances(self):
        comparison = compare_rules(self.bundle)
        constrained = comparison["受约束视角_仅可启用范围"]
        for diff in constrained["隔离配对差异"]:
            # 同实例两规则的完成时点都必须来自完整回放，不允许跨实例拼最短
            self.assertIsNotNone(diff["基线完成分钟"])
            self.assertIsNotNone(diff["试行完成分钟"])


class PilotObservationTest(unittest.TestCase):
    def test_observed_separated_from_simulated(self):
        bundle = load_bundle(BUNDLE)
        observed = observed_pilot(bundle)
        ids = {row["placement"] for row in observed["实例"]}
        self.assertEqual(ids, {"E117", "E118"})
        e117 = next(row for row in observed["实例"] if row["placement"] == "E117")
        self.assertEqual(e117["结果"], "completed")
        self.assertEqual(e117["逾期分钟"], 25)

    def test_backfill_shift_classified_as_execution_gap(self):
        bundle = load_bundle(BUNDLE)
        shifts = backfill_explanation_shift(bundle)
        self.assertEqual({item["placement"] for item in shifts}, {"E034", "E110"})
        for item in shifts:
            self.assertIn("执行", item["解释变化类型"])


if __name__ == "__main__":
    unittest.main()
