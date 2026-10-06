"""按设备期间停机损失结账汇总算法单测（app/closing.py，纯标准库）。

运行：python3 tests/test_closing_alg.py
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.closing import (  # noqa: E402
    AccountSummary,
    empty_totals,
    ensure_account_summary_fits,
    ensure_totals_fit,
    summarize,
)
from app.pricing import DB_AMOUNT_MAX, AmountOverflowError  # noqa: E402


def _reason(p=0, c=0, u=0):
    return {"priced_minutes": p, "amount_cents": c, "unpriced_minutes": u}


def test_empty_totals():
    t = empty_totals()
    assert t.total_cents == 0
    assert t.priced_minutes == 0
    assert t.unpriced_minutes == 0
    assert set(t.by_reason) == {"maintenance", "failure", "pending"}
    assert all(rt.amount_cents == 0 for rt in t.by_reason.values())
    assert summarize([]) == t


def test_summarize_single_account_matches_by_reason():
    acc = AccountSummary(
        total_cents=15000,
        priced_minutes=120,
        unpriced_minutes=30,
        by_reason={
            "maintenance": _reason(p=30, c=6000),
            "failure": _reason(p=90, c=9000),
            "pending": _reason(u=30),
        },
    )
    t = summarize([acc])
    assert t.total_cents == 15000
    assert t.priced_minutes == 120
    assert t.unpriced_minutes == 30
    assert t.by_reason["maintenance"].amount_cents == 6000
    assert t.by_reason["failure"].priced_minutes == 90
    assert t.by_reason["pending"].amount_cents == 0


def test_summarize_multiple_accounts_mechanical_sum():
    # 两笔账（如两个班次）：金额机械相加，不二次取整
    a1 = AccountSummary(
        total_cents=101,
        priced_minutes=10,
        unpriced_minutes=2,
        by_reason={
            "maintenance": _reason(),
            "failure": _reason(p=10, c=101),
            "pending": _reason(u=2),
        },
    )
    a2 = AccountSummary(
        total_cents=205,
        priced_minutes=20,
        unpriced_minutes=0,
        by_reason={
            "maintenance": _reason(p=20, c=205),
            "failure": _reason(),
            "pending": _reason(),
        },
    )
    t = summarize([a1, a2])
    assert t.total_cents == 306
    assert t.priced_minutes == 30
    assert t.unpriced_minutes == 2
    assert t.by_reason["failure"].amount_cents == 101
    assert t.by_reason["maintenance"].amount_cents == 205
    assert t.by_reason["pending"].unpriced_minutes == 2


def test_summarize_missing_reason_key_treated_as_zero():
    acc = AccountSummary(
        total_cents=500,
        priced_minutes=5,
        unpriced_minutes=0,
        by_reason={"failure": _reason(p=5, c=500)},
    )
    t = summarize([acc])
    assert t.total_cents == 500
    assert t.by_reason["maintenance"].amount_cents == 0
    assert t.by_reason["pending"].amount_cents == 0


def test_summarize_rejects_header_detail_mismatch():
    bad = AccountSummary(
        total_cents=500,
        priced_minutes=5,
        unpriced_minutes=0,
        by_reason={"failure": _reason(p=5, c=499)},  # 明细合计不等于账头
    )
    try:
        summarize([bad])
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on header/detail mismatch")


def test_summarize_rejects_negative():
    bad = AccountSummary(
        total_cents=-1,
        priced_minutes=0,
        unpriced_minutes=0,
        by_reason={},
    )
    try:
        summarize([bad])
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on negative totals")


# ---------------------------------------------------------------- BIGINT 金额上界


def _acc(total_cents, failure_cents=0, maintenance_cents=0):
    return AccountSummary(
        total_cents=total_cents,
        priced_minutes=0,
        unpriced_minutes=0,
        by_reason={
            "maintenance": _reason(c=maintenance_cents),
            "failure": _reason(c=failure_cents),
            "pending": _reason(),
        },
    )


def test_ensure_account_summary_fit_boundary():
    ensure_account_summary_fits(_acc(0))
    ensure_account_summary_fits(_acc(DB_AMOUNT_MAX, failure_cents=DB_AMOUNT_MAX))
    try:
        ensure_account_summary_fits(_acc(DB_AMOUNT_MAX + 1))
    except AmountOverflowError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected AmountOverflowError")


def test_ensure_account_summary_fit_detects_by_reason_overflow():
    # 账头标量在界内但按原因汇总越界也拒绝
    try:
        ensure_account_summary_fits(
            AccountSummary(
                total_cents=DB_AMOUNT_MAX + 1,
                priced_minutes=0,
                unpriced_minutes=0,
                by_reason={"failure": _reason(c=DB_AMOUNT_MAX + 1)},
            )
        )
    except AmountOverflowError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected AmountOverflowError")


def test_closing_total_overflow_across_accounts_rejected():
    # 每份账自身在界内（核算固化时已过同界校验），机械求和后整单越界：
    # 不截断、不饱和，抛 AmountOverflowError 由写路径转 422、整单回滚。
    accounts = [
        _acc(5 * 10**18, failure_cents=5 * 10**18),
        _acc(5 * 10**18, failure_cents=5 * 10**18),
    ]
    totals = summarize(accounts)
    assert totals.total_cents == 10**19
    try:
        ensure_totals_fit(totals)
    except AmountOverflowError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected AmountOverflowError on closing total")


def test_closing_totals_in_bounds_pass_unchanged():
    # 边界内金额原样通过、精确保留（无取整/饱和）
    accounts = [
        _acc(10**18, failure_cents=10**18),
        _acc(DB_AMOUNT_MAX - 10**18, failure_cents=DB_AMOUNT_MAX - 10**18),
    ]
    totals = summarize(accounts)
    ensure_totals_fit(totals)
    assert totals.total_cents == DB_AMOUNT_MAX


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed")
