"""按生产批次窗口分摊已固化停机损失账（整分钟半开区间，纯标准库）。

输入
- 一份损失账的全部切分明细（loss_accounting_pieces 同形态）：每行是一个整分钟
  半开区间 [start, end)、单一原因、互不可重叠，按时间顺序排列；priced=TRUE 的
  明细携带"已按分取整"的整数金额 amount_cents，priced=FALSE 的为待定价
  （有分钟、无金额）；
- 一批生产批次窗口：每个批次号一个带时区整分钟半开窗口 [start, end)，批次窗口
  之间互不重叠（端点相接允许）。

分摊规则
- 每个明细与各批次窗口求半开交集：交集分钟归该批次；明细中未被任何批次窗口
  覆盖的分钟归"未分配"（unallocated，单一去向）。所有去向分钟之和恒等于
  该明细分钟；
- **待定价明细只分分钟、不产生金额**（各份额金额为空）；
- **定价明细的整数分按交集分钟占比分配**：份额先取整（floor(金额 × 份额分钟
  ÷ 明细分钟)），余分按小数余数降序逐个 +1；余数相同时批次号升序、未分配
  排在最后；所有去向金额之和恒等于原明细金额（分毫不差）；
- 未分配去向在一个明细内可能对应多个不相邻区间（窗口插在明细中间）：去向金额
  先按上述规则作为一个整体确定，再在其各区间之间用同一套"先取整 + 余数降序
  （区间同余数按起点升序）"机械拆分，使逐行金额之和仍精确等于去向金额——
  按去向/原因的汇总是权威结果，区间拆行仅为来源明细的存储形态。

本模块只依赖标准库，可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .attribution import Interval, intersect, merge, subtract, total_minutes

#: 去向类型
DEST_BATCH = "batch"
DEST_UNALLOCATED = "unallocated"


class AllocationError(ValueError):
    """分摊输入非法：批次号重复、窗口非法或批次窗口互相重叠。"""


@dataclass(frozen=True)
class BatchWindow:
    """一个生产批次的整分钟半开生产窗口。"""

    batch_no: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Piece:
    """损失账的一条切分明细（不可变）。

    priced=True 时 amount_cents 为该明细已按分取整的整数金额；
    priced=False 时为待定价，amount_cents 为 None，只分摊分钟。
    """

    piece_no: int
    seg_no: int | None
    start: datetime
    end: datetime
    reason: str
    minutes: int
    priced: bool
    amount_cents: int | None


@dataclass(frozen=True)
class Split:
    """一条明细 × 一个去向（或未分配的其中一段区间）的分摊行。

    - batch_no 非空（dest=batch）：批次窗口与明细的交集（每批次每明细至多一段）；
    - batch_no 为空（dest=unallocated）：明细落在全部批次窗口之外的一段区间；
    - priced=False 时 amount_cents 为 None（待定价只分分钟）。
    """

    source_piece_no: int
    seg_no: int | None
    dest: str  # batch | unallocated
    batch_no: str | None
    start: datetime
    end: datetime
    reason: str
    minutes: int
    priced: bool
    amount_cents: int | None


@dataclass(frozen=True)
class AllocationResult:
    """一份账的分摊结果：逐行来源明细（未排序，由调用方按起点稳定排序）。"""

    splits: list[Split] = field(default_factory=list)


def validate_windows(windows: list[BatchWindow]) -> None:
    """校验批次窗口：非空、批次号不重复、整分钟半开窗口互不重叠（相接允许）。"""
    if not windows:
        raise AllocationError("at least one batch window is required")
    seen: set[str] = set()
    for w in windows:
        if not w.batch_no:
            raise AllocationError("batch_no must be non-empty")
        if w.batch_no in seen:
            raise AllocationError(f"duplicate batch_no: {w.batch_no!r}")
        seen.add(w.batch_no)
        if w.start.tzinfo is None or w.end.tzinfo is None:
            raise AllocationError(
                f"batch {w.batch_no!r} window must include timezone offsets"
            )
        if w.start.second or w.start.microsecond or w.end.second or w.end.microsecond:
            raise AllocationError(
                f"batch {w.batch_no!r} window must be aligned to whole minutes"
            )
        if not w.start < w.end:
            raise AllocationError(
                f"batch {w.batch_no!r} window must be a non-empty half-open interval"
            )
    ordered = sorted(windows, key=lambda w: (w.start, w.end, w.batch_no))
    prev: BatchWindow | None = None
    for w in ordered:
        if prev is not None and w.start < prev.end:
            raise AllocationError(
                "batch windows must not overlap (half-open [start, end); touching is "
                f"allowed): {prev.batch_no!r} and {w.batch_no!r} overlap"
            )
        prev = w


def _largest_remainder_shares(
    amount: int, weights: list[int]
) -> list[int]:
    """把整数金额按权重分配为整数份：先按比例取整，再按小数余数降序补足余分。

    权重为正整数且 sum(weights) > 0；返回与 weights 等长的整数份，
    恒有 sum(返回) == amount。调用方负责决定同余数的顺序（本函数在权重
    相同时保持输入顺序，故顺序即决胜顺序）。
    """
    total_weight = sum(weights)
    floors = [amount * w // total_weight for w in weights]
    remainders = [(amount * w) % total_weight for w in weights]
    leftover = amount - sum(floors)
    order = sorted(
        range(len(weights)),
        key=lambda i: (-remainders[i], i),
    )
    shares = floors[:]
    for i in order[:leftover]:
        shares[i] += 1
    return shares


def allocate_pieces(
    pieces: list[Piece], windows: list[BatchWindow]
) -> AllocationResult:
    """把一份账的切分明细按批次窗口分摊给各批次与"未分配"。

    windows 必须已通过 validate_windows 校验（互不重叠）。
    """
    validate_windows(windows)
    splits: list[Split] = []

    for piece in sorted(pieces, key=lambda p: (p.start, p.piece_no)):
        # 1) 与各批次窗口求交集（窗口互不重叠 → 交集区间互不重叠）
        batch_intervals: list[tuple[str, Interval, int]] = []
        covered: list[Interval] = []
        for w in windows:
            iv = intersect((piece.start, piece.end), (w.start, w.end))
            if iv is None:
                continue
            minutes = total_minutes([iv])
            if minutes > 0:
                batch_intervals.append((w.batch_no, iv, minutes))
                covered.append(iv)

        # 2) 明细分钟减去窗口覆盖 → 未分配区间（端点相接的相邻差集已排序）
        free_intervals = subtract([(piece.start, piece.end)], merge(covered))

        allocated_minutes = sum(m for _, _, m in batch_intervals) + total_minutes(
            free_intervals
        )
        if allocated_minutes != piece.minutes:
            # 理论不可达：交集 + 差集恒为原区间（半开、整分钟）
            raise AllocationError(
                f"internal error: piece {piece.piece_no} minutes "
                f"{piece.minutes} != allocated {allocated_minutes}"
            )

        if not piece.priced:
            # 待定价：只分分钟，不产生任何金额
            for batch_no, iv, minutes in batch_intervals:
                splits.append(
                    Split(
                        source_piece_no=piece.piece_no,
                        seg_no=piece.seg_no,
                        dest=DEST_BATCH,
                        batch_no=batch_no,
                        start=iv[0],
                        end=iv[1],
                        reason=piece.reason,
                        minutes=minutes,
                        priced=False,
                        amount_cents=None,
                    )
                )
            for iv in free_intervals:
                splits.append(
                    Split(
                        source_piece_no=piece.piece_no,
                        seg_no=piece.seg_no,
                        dest=DEST_UNALLOCATED,
                        batch_no=None,
                        start=iv[0],
                        end=iv[1],
                        reason=piece.reason,
                        minutes=total_minutes([iv]),
                        priced=False,
                        amount_cents=None,
                    )
                )
            continue

        # 3) 定价明细：去向 = 命中批次（按批次号升序决胜）+ 未分配（永远最后）
        amount = piece.amount_cents or 0
        batch_intervals.sort(key=lambda x: x[0])
        free_total = total_minutes(free_intervals)
        weights = [m for _, _, m in batch_intervals]
        if free_total > 0:
            weights.append(free_total)
        cents = _largest_remainder_shares(amount, weights)
        batch_cents = cents[: len(batch_intervals)]
        free_cents_total = cents[len(batch_intervals)] if free_total > 0 else 0

        for (batch_no, iv, minutes), share_cents in zip(
            batch_intervals, batch_cents
        ):
            splits.append(
                Split(
                    source_piece_no=piece.piece_no,
                    seg_no=piece.seg_no,
                    dest=DEST_BATCH,
                    batch_no=batch_no,
                    start=iv[0],
                    end=iv[1],
                    reason=piece.reason,
                    minutes=minutes,
                    priced=True,
                    amount_cents=share_cents,
                )
            )

        if free_total > 0:
            free_mins = [total_minutes([iv]) for iv in free_intervals]
            if len(free_intervals) == 1:
                free_cents_per = [free_cents_total]
            else:
                # 同一去向内部多段：同法机械拆分，同余数按起点升序（free_intervals
                # 已按起点排序）；逐行金额之和精确等于去向金额。
                free_cents_per = _largest_remainder_shares(
                    free_cents_total, free_mins
                )
            for iv, minutes, share_cents in zip(
                free_intervals, free_mins, free_cents_per
            ):
                splits.append(
                    Split(
                        source_piece_no=piece.piece_no,
                        seg_no=piece.seg_no,
                        dest=DEST_UNALLOCATED,
                        batch_no=None,
                        start=iv[0],
                        end=iv[1],
                        reason=piece.reason,
                        minutes=minutes,
                        priced=True,
                        amount_cents=share_cents,
                    )
                )

    return AllocationResult(splits=splits)
