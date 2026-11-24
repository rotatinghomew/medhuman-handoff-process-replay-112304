"""按资料口径只读加载交换包，全部解析为带类型的记录。

口径要点（见 data-contract.md）：
- 空白字段表示来源未提供；集合字段为逗号分隔。
- `arrived` 是记录可用于值班判断的分钟；时钟范围 earliest..latest 只表示可能发生区间。
- supersedes 指向同一业务动作的原始对象。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path


def _int(value: str) -> int | None:
    return int(value) if value.strip() != "" else None


def _list(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip() != "")


@dataclass(frozen=True)
class Placement:
    id: str
    group: str
    site: str
    version: str
    teacher: str
    eligible_min: int
    due_min: int
    state: str


@dataclass(frozen=True)
class LogEvent:
    id: str
    placement: str
    action: str
    actor: str
    earliest: int
    latest: int
    arrived: int
    kind: str
    supersedes: str

    @property
    def is_copy(self) -> bool:
        """copy 仅表示收到副本，不履行接手。"""
        return self.kind == "copy"

    @property
    def is_returned(self) -> bool:
        """returned 结束本次未经确认的交件。"""
        return self.kind == "returned"

    @property
    def is_backfill(self) -> bool:
        """补录：指向同一业务动作的原记录。"""
        return self.supersedes != ""


@dataclass(frozen=True)
class Observability:
    placement: str
    action: str
    from_min: int
    to_min: int
    state: str


@dataclass(frozen=True)
class Workflow:
    version: str
    site_set: tuple[str, ...]
    from_min: int
    scope_required: tuple[str, ...]
    either: tuple[str, ...]
    parallel: tuple[str, ...]
    before_register: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    person: str
    from_min: int
    to_min: int
    role: str
    skills: tuple[str, ...]
    capacity: int


@dataclass(frozen=True)
class Launch:
    site: str
    version: str
    proposed_at: int
    takes_effect: int
    trial_to: int
    state: str

    @property
    def is_announced(self) -> bool:
        """只有 announced 才构成可启用的公示；candidate 仍是候选。"""
        return self.state == "announced"


@dataclass(frozen=True)
class Notice:
    placement: str
    notice_at: int
    version: str
    locked: bool
    promised_route: str


@dataclass
class Bundle:
    placements: dict[str, Placement]
    log: list[LogEvent]
    observability: list[Observability]
    workflows: dict[str, Workflow]
    coverage: list[Coverage]
    launches: list[Launch]
    notices: dict[str, Notice]
    pilot_log: list[LogEvent]
    manifest: dict = field(default_factory=dict)


def _read_csv(root: Path, filename: str) -> list[dict[str, str]]:
    with (root / filename).open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def load_bundle(root: str | Path) -> Bundle:
    """按清单加载全部表，并做最基础的结构校验。"""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    placements = {
        row["id"]: Placement(
            row["id"], row["group"], row["site"], row["version"], row["teacher"],
            int(row["eligible_min"]), int(row["due_min"]), row["state"],
        )
        for row in _read_csv(root, "placement.csv")
    }

    def parse_event(row: dict[str, str]) -> LogEvent:
        return LogEvent(
            row["id"], row["placement"], row["action"], row["actor"],
            int(row["earliest"]), int(row["latest"]), int(row["arrived"]),
            row["kind"], row["supersedes"].strip(),
        )

    log = [parse_event(row) for row in _read_csv(root, "log.csv")]
    pilot_log = [parse_event(row) for row in _read_csv(root, "pilot_log.csv")]

    observability = [
        Observability(
            row["placement"], row["action"], int(row["from_min"]),
            int(row["to_min"]), row["state"],
        )
        for row in _read_csv(root, "observability.csv")
    ]

    workflows = {
        row["version"]: Workflow(
            row["version"], _list(row["site_set"]), int(row["from_min"]),
            _list(row["scope_required"]), _list(row["either"]),
            _list(row["parallel"]), _list(row["before_register"]),
        )
        for row in _read_csv(root, "workflow.csv")
    }

    coverage = [
        Coverage(
            row["person"], int(row["from_min"]), int(row["to_min"]),
            row["role"], _list(row["skills"]), int(row["capacity"]),
        )
        for row in _read_csv(root, "coverage.csv")
    ]

    launches = [
        Launch(
            row["site"], row["version"], int(row["proposed_at"]),
            int(row["takes_effect"]), int(row["trial_to"]), row["state"],
        )
        for row in _read_csv(root, "launch.csv")
    ]

    notices = {
        row["placement"]: Notice(
            row["placement"], int(row["notice_at"]), row["version"],
            row["locked"].strip() == "1", row["promised_route"],
        )
        for row in _read_csv(root, "notice.csv")
    }

    return Bundle(
        placements, log, observability, workflows, coverage,
        launches, notices, pilot_log, manifest,
    )
