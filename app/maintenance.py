"""计划保养核对算法（半开区间，纯标准库）。

输入（全部须已对齐整分钟、带时区，区间一律半开 [start, end)）：
- 计划期间 period = [period_start, period_end)；
- 一批保养窗口 windows：非空、必须落在期间内、互不重叠（端点相接允许）；
- 若干已结算班次在固定变更序号下所选修订的视图：班次区间（与期间的交集构成
  "已结算覆盖"）及其固化归因段（含原因 maintenance/failure/pending）。

逐分钟分类（四类互不相交）：
- 已执行 executed    ：保养窗口 ∩ 保养(maintenance)归因段；
- 未执行 not_executed：保养窗口内已被已结算班次覆盖、但没有保养段的分钟
                       （故障/待判分钟，以及班次内无任何停机归因的分钟）；
- 未核实 unverified  ：保养窗口中未被任何已结算班次覆盖的分钟（单列，
                       不计未执行——无法据已结算数据判定）；
- 计划外 unplanned   ：期间内、保养窗口之外出现的保养段分钟。

本模块只依赖标准库，可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .attribution import Interval, intersect, merge, subtract, total_minutes


class MaintenancePlanError(ValueError):
    """保养计划内容非法（空窗口、越出期间、窗口互相重叠等）。"""


@dataclass(frozen=True)
class ShiftView:
    """一个已结算班次对核对期间的贡献。

    coverage 为班次区间（调用前可给完整班次区间，本模块会裁剪到计划期间）；
    segments 为该班次按固定序号选定修订的固化归因段 [(区间, 原因), ...]。
    """

    shift_id: int
    revision: int
    seq: int
    coverage: Interval
    segments: list[tuple[Interval, str]]


@dataclass(frozen=True)
class MaintenanceCheckResult:
    """核对结果：四类最大连续区间（均按时间顺序）及各自分钟总数。"""

    executed: list[Interval] = field(default_factory=list)
    not_executed: list[Interval] = field(default_factory=list)
    unplanned: list[Interval] = field(default_factory=list)
    unverified: list[Interval] = field(default_factory=list)
    executed_minutes: int = 0
    not_executed_minutes: int = 0
    unplanned_minutes: int = 0
    unverified_minutes: int = 0
    covered_minutes: int = 0  # 期间内已结算班次覆盖的分钟总数（核对依据范围）


def normalize_windows(
    period: Interval, windows: list[Interval]
) -> list[Interval]:
    """校验保养窗口并返回按起点排序的规范列表。

    - 窗口列表非空；
    - 每个窗口必须完整落在期间 [period_start, period_end) 内
      （窗口端点可与期间端点相等）；
    - 窗口之间不得重叠（半开：端点相接允许，如 [a,b) 与 [b,c) 合法）。
    """
    period_start, period_end = period
    if not windows:
        raise MaintenancePlanError("maintenance windows must not be empty")
    norm: list[Interval] = []
    for idx, (s, e) in enumerate(windows):
        if s >= e:
            raise MaintenancePlanError(
                f"window #{idx + 1} is invalid: start must be earlier than end"
            )
        if s < period_start or e > period_end:
            raise MaintenancePlanError(
                f"window #{idx + 1} [{s.isoformat()}, {e.isoformat()}) is not "
                f"within plan period "
                f"[{period_start.isoformat()}, {period_end.isoformat()})"
            )
        norm.append((s, e))
    norm.sort()
    for (s1, e1), (s2, e2) in zip(norm, norm[1:]):
        if s2 < e1:
            raise MaintenancePlanError(
                f"maintenance windows overlap: [{s1.isoformat()}, {e1.isoformat()}) "
                f"and [{s2.isoformat()}, {e2.isoformat()})"
            )
    return norm


def _intersect_union(
    intervals: list[Interval], covers: list[Interval]
) -> list[Interval]:
    """两组半开区间并集的交集（返回合并后的最大连续区间）。"""
    out: list[Interval] = []
    merged_covers = merge(covers)
    for iv in intervals:
        for c in merged_covers:
            x = intersect(iv, c)
            if x is not None:
                out.append(x)
    return merge(out)


def check_maintenance(
    period: Interval,
    windows: list[Interval],
    shifts: list[ShiftView],
) -> MaintenanceCheckResult:
    """按已结算班次修订视图核对计划保养窗口的执行情况。

    windows 应已通过 normalize_windows 校验（本函数仍会合并，容错重叠相接）；
    shifts 中各班次的 coverage/segments 一律先裁剪到计划期间再参与运算。
    """
    period_start, period_end = period
    window_union = merge(windows)

    coverage_raw: list[Interval] = []
    maintenance_raw: list[Interval] = []
    for sh in shifts:
        cov = intersect(sh.coverage, (period_start, period_end))
        if cov is not None:
            coverage_raw.append(cov)
        for (s, e), reason in sh.segments:
            iv = intersect((s, e), (period_start, period_end))
            if iv is not None and reason == "maintenance":
                maintenance_raw.append(iv)

    coverage = merge(coverage_raw)  # 已结算覆盖（同设备班次不重叠，合并仅为统一形态）
    maintenance = merge(maintenance_raw)  # 固化保养段并集（恒在覆盖内）

    # 已执行：窗口 ∩ 保养段
    executed = _intersect_union(window_union, maintenance)
    # 窗口内被已结算班次覆盖的分钟
    covered_in_window = _intersect_union(window_union, coverage)
    # 未执行：窗口内已覆盖但非保养（故障/待判/无停机归因）
    not_executed = subtract(covered_in_window, maintenance)
    # 未核实：窗口内未被任何已结算班次覆盖（不计未执行）
    unverified = subtract(window_union, coverage)
    # 计划外：期间内窗口外的保养段
    unplanned = subtract(maintenance, window_union)

    return MaintenanceCheckResult(
        executed=executed,
        not_executed=not_executed,
        unplanned=unplanned,
        unverified=unverified,
        executed_minutes=total_minutes(executed),
        not_executed_minutes=total_minutes(not_executed),
        unplanned_minutes=total_minutes(unplanned),
        unverified_minutes=total_minutes(unverified),
        covered_minutes=total_minutes(_intersect_union(coverage, [(period_start, period_end)])),
    )
