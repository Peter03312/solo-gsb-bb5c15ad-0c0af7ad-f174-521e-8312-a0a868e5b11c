"""按设备期间停机损失结账：账头按原因汇总（纯标准库）。

结账把若干已固化损失账（每份 loss_accountings 的合计与 by_reason 同形态）
机械汇总为整单的金额、定价分钟与待定价分钟，并按原因给出汇总。规则：

- 金额（整数分）直接相加：每份账的合计已是"逐段按分四舍五入后求和"，
  结账不做二次取整，整单金额恒等于全部纳入损失账合计之和；
- 定价分钟、待定价分钟分别相加；三类原因（保养/故障/待判）恒列出，
  无任何账时为全 0 的空汇总（空结账）；
- 结账不重新计价，只汇总既有账：形成或复用损失账的逐段计价与待定价规则
  完全沿用 pricing.price_segments。

本模块只依赖标准库，可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from .attribution import REASONS


@dataclass(frozen=True)
class AccountSummary:
    """一份纳入结账的损失账的合计视图（与 loss_accountings 列同形态）。

    by_reason 形如：
    {"maintenance": {"priced_minutes", "amount_cents", "unpriced_minutes"}, ...}
    缺失的原因按 0 处理（兼容只写了部分原因键的历史数据）。
    """

    total_cents: int
    priced_minutes: int
    unpriced_minutes: int
    by_reason: dict[str, dict]


@dataclass(frozen=True)
class ReasonTotal:
    reason: str
    priced_minutes: int = 0
    amount_cents: int = 0
    unpriced_minutes: int = 0


@dataclass(frozen=True)
class ClosingTotals:
    """整单汇总：金额/定价分钟/待定价分钟及按原因汇总（三类原因恒列出）。"""

    total_cents: int
    priced_minutes: int
    unpriced_minutes: int
    by_reason: dict[str, ReasonTotal]


def empty_totals() -> ClosingTotals:
    """全 0 汇总（无符合班次的空结账）。"""
    return ClosingTotals(
        total_cents=0,
        priced_minutes=0,
        unpriced_minutes=0,
        by_reason={r: ReasonTotal(reason=r) for r in REASONS},
    )


def summarize(accounts: list[AccountSummary]) -> ClosingTotals:
    """把多份损失账合计机械汇总为整单合计（不重新计价、不二次取整）。

    同时校验各账头标量与其 by_reason 明细之和一致（防御固化数据异常）。
    """
    totals = {r: ReasonTotal(reason=r) for r in REASONS}
    total_cents = 0
    total_priced = 0
    total_unpriced = 0
    for acc in accounts:
        if acc.total_cents < 0 or acc.priced_minutes < 0 or acc.unpriced_minutes < 0:
            raise ValueError("loss accounting totals must be non-negative")
        sum_cents = 0
        sum_priced = 0
        sum_unpriced = 0
        for reason in REASONS:
            item = acc.by_reason.get(reason) or {}
            priced = int(item.get("priced_minutes", 0))
            unpriced = int(item.get("unpriced_minutes", 0))
            cents = int(item.get("amount_cents", 0))
            if priced < 0 or unpriced < 0 or cents < 0:
                raise ValueError("loss accounting by_reason values must be non-negative")
            sum_cents += cents
            sum_priced += priced
            sum_unpriced += unpriced
            prev = totals[reason]
            totals[reason] = ReasonTotal(
                reason=reason,
                priced_minutes=prev.priced_minutes + priced,
                amount_cents=prev.amount_cents + cents,
                unpriced_minutes=prev.unpriced_minutes + unpriced,
            )
        if (
            sum_cents != acc.total_cents
            or sum_priced != acc.priced_minutes
            or sum_unpriced != acc.unpriced_minutes
        ):
            raise ValueError(
                "loss accounting header totals do not match the sum of by_reason rows"
            )
        total_cents += acc.total_cents
        total_priced += acc.priced_minutes
        total_unpriced += acc.unpriced_minutes
    return ClosingTotals(
        total_cents=total_cents,
        priced_minutes=total_priced,
        unpriced_minutes=total_unpriced,
        by_reason=totals,
    )
