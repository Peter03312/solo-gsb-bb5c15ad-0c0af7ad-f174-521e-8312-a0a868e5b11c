"""按设备期间停机损失结账汇总算法单测（app/closing.py，纯标准库）。

运行：python3 tests/test_closing_alg.py
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.closing import (  # noqa: E402
    AccountSummary,
    empty_totals,
    summarize,
)


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed")
