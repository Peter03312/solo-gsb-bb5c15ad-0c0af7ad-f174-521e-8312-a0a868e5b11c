"""按批次分摊停机损失账算法单元测试（仅依赖标准库）。

可用 pytest 运行，也可直接：python3 tests/test_allocation.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import pytest
except ImportError:  # 直跑（python3 tests/test_allocation.py）时的最小 shim
    class _Raises:
        def __init__(self, exc):
            self.exc = exc

        def __enter__(self):
            return self

        def __exit__(self, etype, e, tb):
            return etype is not None and issubclass(etype, self.exc)

    class _Pytest:
        @staticmethod
        def raises(exc):
            return _Raises(exc)

    pytest = _Pytest()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.allocation import (  # noqa: E402
    AllocationError,
    BatchWindow,
    Piece,
    allocate_pieces,
    validate_windows,
)

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 1, 0, 0, tzinfo=TZ)


def t(hours: float = 0, minutes: float = 0) -> datetime:
    return BASE + timedelta(hours=hours, minutes=minutes)


def piece(no, sh, eh, reason="failure", cents=100, priced=True, seg_no=1):
    minutes = int((eh - sh).total_seconds()) // 60
    return Piece(
        piece_no=no,
        seg_no=seg_no if priced else None,
        start=sh,
        end=eh,
        reason=reason,
        minutes=minutes,
        priced=priced,
        amount_cents=cents if priced else None,
    )


def win(bno, sh, eh):
    return BatchWindow(batch_no=bno, start=sh, end=eh)


def _by_dest(splits):
    out = {}
    for s in splits:
        key = ("unallocated", None) if s.dest == "unallocated" else ("batch", s.batch_no)
        out.setdefault(key, []).append(s)
    return out


# ---------------------------------------------------------------- 窗口校验


def test_windows_touching_allowed_but_overlapping_rejected():
    validate_windows([win("A", t(10), t(11)), win("B", t(11), t(12))])
    with pytest.raises(AllocationError):
        validate_windows([win("A", t(10), t(11, 30)), win("B", t(11), t(12))])


def test_windows_duplicate_batch_no_rejected():
    with pytest.raises(AllocationError):
        validate_windows([win("A", t(10), t(11)), win("A", t(11), t(12))])


def test_windows_illegal_window_rejected():
    # 缺时区
    naive = datetime(2026, 10, 1, 10, 0)
    with pytest.raises(AllocationError):
        validate_windows([win("A", naive, t(11))])
    # 未对齐整分钟
    with pytest.raises(AllocationError):
        validate_windows(
            [win("A", t(10).replace(second=30), t(11))]
        )
    # 空窗口 / 倒置
    with pytest.raises(AllocationError):
        validate_windows([win("A", t(11), t(11))])
    with pytest.raises(AllocationError):
        validate_windows([win("A", t(12), t(11))])
    # 空批次列表
    with pytest.raises(AllocationError):
        validate_windows([])


def test_overlapping_windows_checked_even_when_out_of_order():
    with pytest.raises(AllocationError):
        allocate_pieces(
            [piece(1, t(10), t(12))],
            [win("A", t(10), t(12)), win("B", t(11), t(13))],
        )


# ---------------------------------------------------------------- 分钟交集


def test_minutes_split_to_batches_and_unallocated():
    res = allocate_pieces(
        [piece(1, t(10), t(13), cents=0)],
        [win("B", t(11), t(12)), win("A", t(10), t(11))],
    )
    groups = _by_dest(res.splits)
    assert [s.minutes for s in groups[("batch", "A")]] == [60]
    assert [s.minutes for s in groups[("batch", "B")]] == [60]
    assert [s.minutes for s in groups[("unallocated", None)]] == [60]
    total = sum(s.minutes for s in res.splits)
    assert total == 180


def test_window_outside_piece_all_unallocated():
    res = allocate_pieces(
        [piece(1, t(10), t(11))],
        [win("A", t(14), t(15))],
    )
    groups = _by_dest(res.splits)
    assert ("batch", "A") not in groups
    assert sum(s.minutes for s in groups[("unallocated", None)]) == 60


def test_unallocated_splits_into_multiple_segments_when_window_in_middle():
    res = allocate_pieces(
        [piece(1, t(10), t(13), cents=10)],
        [win("A", t(11), t(12))],
    )
    free = sorted(
        (s for s in res.splits if s.dest == "unallocated"),
        key=lambda s: s.start,
    )
    assert [(s.minutes) for s in free] == [60, 60]
    # 逐行金额之和（去向拆分）与分钟守恒
    assert sum(s.minutes for s in res.splits) == 180
    assert sum(s.amount_cents for s in res.splits) == 10


# ---------------------------------------------------------------- 金额分配


def test_priced_cents_proportional_floor_and_remainder_order():
    # 明细 180 分钟、10 分；三个批次各 60 分钟 → 每份 floor 3，余 1 分，
    # 余数相同 → 批次号升序 A 得 +1；未分配不参与（全部覆盖）。
    res = allocate_pieces(
        [piece(1, t(10), t(13), cents=10)],
        [win("C", t(12), t(13)), win("B", t(11), t(12)), win("A", t(10), t(11))],
    )
    cents = {s.batch_no: s.amount_cents for s in res.splits}
    assert cents == {"A": 4, "B": 3, "C": 3}
    assert sum(cents.values()) == 10


def test_unallocated_ranks_last_on_equal_remainder():
    # 明细 120 分钟、10 分；批次 A 覆盖 60 分钟，未分配 60 分钟：
    # 理论各 5 分无余分；改成 11 分 → floor 5/5，余 1，余数相同
    # （各 60 分钟）→ 批次优先，A=6，未分配=5。
    res = allocate_pieces(
        [piece(1, t(10), t(12), cents=11)],
        [win("A", t(10), t(11))],
    )
    groups = _by_dest(res.splits)
    assert sum(s.amount_cents for s in groups[("batch", "A")]) == 6
    assert sum(s.amount_cents for s in groups[("unallocated", None)]) == 5


def test_larger_remainder_wins_regardless_of_batch_order():
    # 明细 60 分钟、10 分：A 占 42 分钟（理论 7.0），未分配 18 分钟（理论 3.0）
    # 无余分；改用 8 分：A=5.6→5（余 .6），未分配=2.4→2（余 .4），
    # 余分 1 归余数更大的批次 → A=6，未分配=2，合计 8。
    res = allocate_pieces(
        [piece(1, t(10), t(11), cents=8)],
        [win("A", t(10), t(10, 42))],
    )
    groups = _by_dest(res.splits)
    a = sum(s.amount_cents for s in groups[("batch", "A")])
    u = sum(s.amount_cents for s in groups[("unallocated", None)])
    assert (a, u) == (6, 2)


def test_zero_amount_distributes_zero():
    res = allocate_pieces(
        [piece(1, t(10), t(12), cents=0)],
        [win("A", t(10), t(11)), win("B", t(11), t(11, 30))],
    )
    assert all(s.amount_cents == 0 for s in res.splits)


def test_cents_conservation_across_many_pieces():
    pieces = [
        piece(1, t(9), t(10), cents=1),
        piece(2, t(10), t(12), cents=12345, seg_no=2),
        piece(3, t(13), t(14), cents=7, seg_no=3),
    ]
    windows = [
        win("B", t(9, 20), t(10, 40)),
        win("A", t(13), t(13, 10)),
        win("C", t(15), t(16)),  # 与所有明细不相交
    ]
    res = allocate_pieces(pieces, windows)
    for p in pieces:
        rows = [s for s in res.splits if s.source_piece_no == p.piece_no]
        assert sum(s.amount_cents for s in rows) == p.amount_cents
        assert sum(s.minutes for s in rows) == p.minutes


# ---------------------------------------------------------------- 待定价


def test_unpriced_piece_allocates_minutes_only():
    res = allocate_pieces(
        [piece(1, t(10), t(12), reason="maintenance", cents=None, priced=False, seg_no=None)],
        [win("A", t(10), t(11))],
    )
    for s in res.splits:
        assert s.priced is False
        assert s.amount_cents is None
    groups = _by_dest(res.splits)
    assert sum(s.minutes for s in groups[("batch", "A")]) == 60
    assert sum(s.minutes for s in groups[("unallocated", None)]) == 60


# ---------------------------------------------------------------- 多时区窗口


def test_window_with_other_offset_matches_by_instant():
    # 批次窗口用 UTC 给出 03:00–04:00Z == 北京 11:00–12:00
    utc_win = BatchWindow(
        batch_no="A",
        start=datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc),
        end=datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc),
    )
    res = allocate_pieces([piece(1, t(10), t(12), cents=10)], [utc_win])
    groups = _by_dest(res.splits)
    assert sum(s.minutes for s in groups[("batch", "A")]) == 60
    assert sum(s.amount_cents for s in groups[("batch", "A")]) == 5
    assert sum(s.amount_cents for s in groups[("unallocated", None)]) == 5


if __name__ == "__main__":
    import inspect

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    raise SystemExit(1 if failures else 0)
