"""计划保养核对算法单元测试（仅依赖标准库）。

可用 pytest 运行，也可直接：python3 tests/test_maintenance_alg.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.maintenance import (  # noqa: E402
    MaintenancePlanError,
    ShiftView,
    check_maintenance,
    normalize_windows,
)

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def t(hours=0, minutes=0):
    return BASE + timedelta(hours=hours, minutes=minutes)


def mins(a, b):
    return (BASE + timedelta(minutes=a), BASE + timedelta(minutes=b))


PERIOD = (t(0), t(24))


def sv(shift_id, coverage, segments, revision=1, seq=1):
    return ShiftView(
        shift_id=shift_id,
        revision=revision,
        seq=seq,
        coverage=coverage,
        segments=list(segments),
    )


# ---------------------------------------------------------------- normalize_windows


def test_normalize_windows_sorted():
    ws = normalize_windows(PERIOD, [mins(120, 180), mins(60, 90)])
    assert ws == [mins(60, 90), mins(120, 180)]


def test_normalize_windows_adjacent_allowed():
    assert normalize_windows(PERIOD, [mins(60, 90), mins(90, 120)]) == [
        mins(60, 90),
        mins(90, 120),
    ]


def test_normalize_windows_empty():
    with pytest.raises(MaintenancePlanError):
        normalize_windows(PERIOD, [])


def test_normalize_windows_out_of_period():
    with pytest.raises(MaintenancePlanError):
        normalize_windows(PERIOD, [mins(-10, 30)])
    with pytest.raises(MaintenancePlanError):
        normalize_windows(PERIOD, [mins(1400, 1500)])


def test_normalize_windows_touching_period_edges_ok():
    assert normalize_windows(PERIOD, [mins(0, 60), mins(1380, 1440)]) == [
        mins(0, 60),
        mins(1380, 1440),
    ]


def test_normalize_windows_overlap_rejected():
    with pytest.raises(MaintenancePlanError):
        normalize_windows(PERIOD, [mins(60, 120), mins(90, 150)])


def test_normalize_windows_invalid_interval():
    with pytest.raises(MaintenancePlanError):
        normalize_windows(PERIOD, [mins(120, 60)])


# ---------------------------------------------------------------- check_maintenance


def test_all_executed():
    # 班次 08-16，保养段 10-12；计划窗口 10-12 → 全部已执行
    shifts = [sv(1, (t(8), t(16)), [((t(10), t(12)), "maintenance")])]
    r = check_maintenance(PERIOD, [mins(600, 720)], shifts)
    assert r.executed == [mins(600, 720)]
    assert r.executed_minutes == 120
    assert r.not_executed == []
    assert r.unplanned == []
    assert r.unverified == []
    assert r.covered_minutes == 480


def test_window_with_failure_minutes_is_not_executed():
    # 窗口 10-12：10-11 保养、11-12 故障 → 已执行 60，未执行 60
    shifts = [
        sv(
            1,
            (t(8), t(16)),
            [
                ((t(10), t(11)), "maintenance"),
                ((t(11), t(12)), "failure"),
            ],
        )
    ]
    r = check_maintenance(PERIOD, [mins(600, 720)], shifts)
    assert r.executed == [mins(600, 660)]
    assert r.not_executed == [mins(660, 720)]
    assert r.executed_minutes == 60
    assert r.not_executed_minutes == 60


def test_window_in_normal_production_minutes_is_not_executed():
    # 窗口 10-12，班次 08-16 内无任何停机归因 → 120 分钟全部未执行（已结算但非保养）
    shifts = [sv(1, (t(8), t(16)), [])]
    r = check_maintenance(PERIOD, [mins(600, 720)], shifts)
    assert r.executed == []
    assert r.not_executed == [mins(600, 720)]
    assert r.not_executed_minutes == 120
    assert r.unverified == []


def test_window_outside_settled_shifts_is_unverified():
    # 窗口 10-12，无班次覆盖 → 未核实 120（不计未执行）
    r = check_maintenance(PERIOD, [mins(600, 720)], [])
    assert r.unverified == [mins(600, 720)]
    assert r.unverified_minutes == 120
    assert r.not_executed == []
    assert r.executed == []
    assert r.covered_minutes == 0


def test_partial_coverage_splits_window():
    # 窗口 10-12；班次仅覆盖 10-11（保养段 10-11）→ 已执行 60 + 未核实 60
    shifts = [
        sv(1, (t(10), t(11)), [((t(10), t(11)), "maintenance")])
    ]
    r = check_maintenance(PERIOD, [mins(600, 720)], shifts)
    assert r.executed == [mins(600, 660)]
    assert r.not_executed == []
    assert r.unverified == [mins(660, 720)]
    assert r.unverified_minutes == 60


def test_unplanned_maintenance_outside_windows():
    # 窗口 10-11（已执行）；另有保养段 14-15 在期间内窗口外 → 计划外
    shifts = [
        sv(
            1,
            (t(8), t(16)),
            [
                ((t(10), t(11)), "maintenance"),
                ((t(14), t(15)), "maintenance"),
            ],
        )
    ]
    r = check_maintenance(PERIOD, [mins(600, 660)], shifts)
    assert r.executed == [mins(600, 660)]
    assert r.unplanned == [mins(840, 900)]
    assert r.unplanned_minutes == 60


def test_window_union_and_multiple_segments_merge_to_maximal():
    # 两个相接窗口 10-11、11-12，保养段同样相接 → 已执行合并为最大连续区间
    shifts = [
        sv(
            1,
            (t(8), t(16)),
            [((t(10), t(11)), "maintenance"), ((t(11), t(12)), "maintenance")],
        )
    ]
    r = check_maintenance(PERIOD, [mins(600, 660), mins(660, 720)], shifts)
    assert r.executed == [mins(600, 720)]
    assert r.executed_minutes == 120


def test_two_shifts_adjacent_coverage():
    # 班次 08-12（保养 10-12）+ 班次 12-16（无停机）；窗口 10-13
    shifts = [
        sv(1, (t(8), t(12)), [((t(10), t(12)), "maintenance")], revision=1, seq=1),
        sv(2, (t(12), t(16)), [], revision=3, seq=7),
    ]
    r = check_maintenance(PERIOD, [mins(600, 780)], shifts)
    assert r.executed == [mins(600, 720)]
    assert r.not_executed == [mins(720, 780)]
    assert r.unverified == []
    assert r.covered_minutes == 480


def test_clipping_to_period():
    # 班次/段超出期间时裁剪：期间 08-10，班次 06-12，保养 09-11
    period = (t(8), t(10))
    shifts = [sv(1, (t(6), t(12)), [((t(9), t(11)), "maintenance")])]
    r = check_maintenance(period, [(t(8), t(10))], shifts)
    # 窗口 08-10：已执行 09-10，未执行 08-09；计划外保养在期间内窗口外 → 无
    assert r.executed == [(t(9), t(10))]
    assert r.not_executed == [(t(8), t(9))]
    assert r.unplanned == []
    assert r.covered_minutes == 120


def test_pending_minutes_in_window_not_executed():
    shifts = [sv(1, (t(8), t(16)), [((t(10), t(12)), "pending")])]
    r = check_maintenance(PERIOD, [mins(600, 720)], shifts)
    assert r.executed == []
    assert r.not_executed == [mins(600, 720)]


def test_four_categories_partition_window_minutes():
    # 窗口 09-15：
    #   09-10 无班次（未核实）
    #   10-11 班次内保养（已执行）
    #   11-12 班次内故障（未执行）
    #   12-13 班次内正常生产（未执行）
    #   13-15 无班次（未核实）
    shifts = [
        sv(
            1,
            (t(10), t(13)),
            [
                ((t(10), t(11)), "maintenance"),
                ((t(11), t(12)), "failure"),
            ],
        )
    ]
    r = check_maintenance(PERIOD, [mins(540, 900)], shifts)
    assert r.executed == [mins(600, 660)]
    assert r.not_executed == [mins(660, 780)]
    assert r.unverified == [mins(540, 600), mins(780, 900)]
    assert (
        r.executed_minutes
        + r.not_executed_minutes
        + r.unverified_minutes
        == 360
    )
