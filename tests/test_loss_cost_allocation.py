"""按生产批次分摊停机损失（POST /devices/{id}/loss-cost-allocations）端到端测试。

覆盖：整分钟窗口交集分钟分摊、定价明细整数分按占比先取整再余数补足
（同余数批次号升序、未分配最后）、待定价只分分钟、金额/分钟守恒、
各账各去向按原因汇总与来源明细、同单号同内容重试幂等（顺序无关）、
同单号异内容冲突、未知/跨设备账、同班次多账、批次窗口重叠/重复/非法、
整单拒绝不留部分结果、历史分摊随后续修订/费率发布不变、读取与列表。

运行方式：docker compose exec app pytest tests/test_loss_cost_allocation.py
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "loss_cost_allocation_splits",
    "loss_cost_allocation_accounts",
    "loss_cost_allocations",
    "loss_accounting_pieces",
    "loss_accountings",
    "rate_entries",
    "rate_versions",
    "manual_adjudications",
    "shift_result_segments",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)

DEVICE = {"id": "CNC-01", "name": "加工中心1号"}


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


# ---------------------------------------------------------------- 构造数据


def _event(device, source, version, sh, eh, reason, day="01"):
    return {
        "device_id": device,
        "source_id": source,
        "version": version,
        "start": f"2026-10-{day}T{sh}+08:00",
        "end": f"2026-10-{day}T{eh}+08:00",
        "reason": reason,
    }


def _rate_entry(reason, sh, eh, amount, day="01"):
    return {
        "reason": reason,
        "start": f"2026-10-{day}T{sh}+08:00",
        "end": f"2026-10-{day}T{eh}+08:00",
        "amount_per_minute": amount,
    }


def _setup_accounting(client) -> tuple[int, int]:
    """完整主流程：设备 → 班次 → 事件 → 结算 → 费率 v1 → 核算。"""
    client.post("/devices", json=DEVICE)
    r = client.post(
        "/shifts",
        json={
            "device_id": "CNC-01",
            "start": "2026-10-01T08:00:00+08:00",
            "end": "2026-10-01T16:00:00+08:00",
        },
    )
    shift_id = r.json()["id"]
    client.post(
        "/events",
        json=_event("CNC-01", "PLC-9", 1, "10:00:00", "12:00:00", "failure"),
    )
    client.post(
        "/events",
        json=_event("CNC-01", "MES-1", 1, "11:00:00", "11:30:00", "maintenance"),
    )
    client.post(f"/shifts/{shift_id}/settle")
    client.post(
        "/devices/CNC-01/rate-versions",
        json={"entries": [_rate_entry("failure", "00:00:00", "23:59:00", "1")]},
    )
    r = client.post(
        f"/shifts/{shift_id}/loss-accountings", json={"rate_version": 1}
    )
    assert r.status_code == 201, r.text
    return shift_id, r.json()["id"]


def _allocate(client, body, device="CNC-01", expect=None):
    r = client.post(f"/devices/{device}/loss-cost-allocations", json=body)
    if expect is not None:
        assert r.status_code == expect, r.text
    return r


def _batch(bno, sh, eh, day="01"):
    return {
        "batch_no": bno,
        "start": f"2026-10-{day}T{sh}+08:00",
        "end": f"2026-10-{day}T{eh}+08:00",
    }


# ---------------------------------------------------------------- 主流程


def test_allocate_minutes_and_cents_with_destination_reason_totals(client):
    _shift_id, accounting_id = _setup_accounting(client)
    # 批次 A 10:00-11:00（60 分钟故障），B 11:00-12:00（含保养 30 待定价 +
    # 故障 11:30-12:00 30 分钟）；窗口外无分钟（账内停机恰好全覆盖）。
    body = {
        "device_id": "CNC-01",
        "allocation_no": "AL-001",
        "accounting_ids": [accounting_id],
        "batches": [
            _batch("B-A", "11:00:00", "12:00:00"),
            _batch("B-B", "13:00:00", "14:00:00"),  # 与停机不相交
            _batch("B-A2", "10:00:00", "11:00:00"),
        ],
    }
    r = _allocate(client, body, expect=201)
    data = r.json()
    assert data["idempotent"] is False
    account = data["accounts"][0]
    assert account["accounting_id"] == accounting_id

    dest = {(d["dest"], d["batch_no"]): d for d in account["destinations"]}
    # A：故障 60 分钟定价 60.00
    a = dest[("batch", "B-A2")]
    assert a["priced_minutes"] == 60
    assert a["amount"] == "60.00"
    by = {x["reason"]: x for x in a["by_reason"]}
    assert by["failure"]["amount_cents"] == 6000
    # B：故障 30（30.00）+ 保养待定价 30（无金额）
    b = dest[("batch", "B-A")]
    assert b["priced_minutes"] == 30
    assert b["unpriced_minutes"] == 30
    assert b["amount"] == "30.00"
    byb = {x["reason"]: x for x in b["by_reason"]}
    assert byb["failure"]["amount_cents"] == 3000
    assert byb["maintenance"]["unpriced_minutes"] == 30
    assert byb["maintenance"]["amount_cents"] == 0
    # 不相交批次：全 0
    empty = dest[("batch", "B-B")]
    assert empty["priced_minutes"] == 0 and empty["amount"] == "0.00"
    # 未分配：无（停机全部被 A/B 覆盖）
    u = dest[("unallocated", None)]
    assert u["priced_minutes"] == 0 and u["unpriced_minutes"] == 0

    # 合计守恒：账 totals 不变；各去向金额之和 = 账金额；分钟之和 = 账分钟
    assert account["totals"]["amount"] == "90.00"
    assert sum(d["amount_cents"] for d in account["destinations"]) == 9000
    assert (
        sum(d["priced_minutes"] for d in account["destinations"])
        == account["totals"]["priced_minutes"]
        == 90
    )
    assert (
        sum(d["unpriced_minutes"] for d in account["destinations"])
        == account["totals"]["unpriced_minutes"]
        == 30
    )

    # 来源明细：定价行有金额、待定价行金额为 null；splits 分钟/金额守恒
    splits = account["splits"]
    priced = [s for s in splits if s["priced"]]
    unpriced = [s for s in splits if not s["priced"]]
    assert sum(s["minutes"] for s in splits) == 120
    assert sum(s["amount_cents"] for s in priced) == 9000
    assert all(s["amount"] is None and s["amount_cents"] is None for s in unpriced)
    assert {(s["dest"], s["batch_no"], s["reason"]) for s in unpriced} == {
        ("batch", "B-A", "maintenance")
    }
    # split_no 单内连续
    assert [s["split_no"] for s in splits] == list(range(1, len(splits) + 1))


def test_out_of_window_minutes_go_unallocated_with_tie_break_last(client):
    _shift_id, accounting_id = _setup_accounting(client)
    # 只放一个窗口覆盖故障段 10:00-11:00（60 分钟，6000 分）；
    # 其余故障 11:30-12:00（3000 分）与保养待定价 30 分钟全部未分配。
    body = {
        "device_id": "CNC-01",
        "allocation_no": "AL-002",
        "accounting_ids": [accounting_id],
        "batches": [_batch("B1", "10:00:00", "11:00:00")],
    }
    data = _allocate(client, body, expect=201).json()
    account = data["accounts"][0]
    dest = {(d["dest"], d["batch_no"]): d for d in account["destinations"]}
    assert dest[("batch", "B1")]["amount"] == "60.00"
    u = dest[("unallocated", None)]
    assert u["priced_minutes"] == 30
    assert u["unpriced_minutes"] == 30
    assert u["amount"] == "30.00"
    byu = {x["reason"]: x for x in u["by_reason"]}
    assert byu["failure"]["amount_cents"] == 3000
    assert byu["maintenance"]["unpriced_minutes"] == 30


def test_cents_remainder_tie_break_batch_then_unallocated(client):
    """单条 3 分钟、合计 10 分的定价明细在三个等分钟去向间分配。

    发布整段费率 0.033333 元/分：段金额 0.033333×3 = 0.099999 元
    （99999 微元）按分 ROUND_HALF_UP → 10 分，形成"一条明细 3 分钟 10 分"。
    floor 各 3 分，余 1 分，三方余数相同（各占 1 分钟 = 1/3）→
    批次号升序先补、未分配永远最后：B1=4、B2=3、未分配=3。
    """
    client.post("/devices", json=DEVICE)
    r = client.post(
        "/shifts",
        json={
            "device_id": "CNC-01",
            "start": "2026-10-01T08:00:00+08:00",
            "end": "2026-10-01T16:00:00+08:00",
        },
    )
    shift_id = r.json()["id"]
    client.post(
        "/events",
        json=_event("CNC-01", "P", 1, "10:00:00", "10:03:00", "failure"),
    )
    client.post(f"/shifts/{shift_id}/settle")
    client.post(
        "/devices/CNC-01/rate-versions",
        json={"entries": [_rate_entry("failure", "10:00:00", "10:03:00", "0.033333")]},
    )
    acc = client.post(
        f"/shifts/{shift_id}/loss-accountings", json={"rate_version": 1}
    ).json()
    assert acc["totals"]["amount_cents"] == 10
    assert len(acc["priced_pieces"]) == 1  # 单一 3 分钟定价明细

    body = {
        "device_id": "CNC-01",
        "allocation_no": "AL-003",
        "accounting_ids": [acc["id"]],
        # 故意乱序提交，批次决胜仍按批次号升序；10:02-10:03 无窗口 → 未分配
        "batches": [
            _batch("B2", "10:01:00", "10:02:00"),
            _batch("B1", "10:00:00", "10:01:00"),
        ],
    }
    data = _allocate(client, body, expect=201).json()
    account = data["accounts"][0]
    dest = {(d["dest"], d["batch_no"]): d for d in account["destinations"]}
    assert dest[("batch", "B1")]["amount_cents"] == 4
    assert dest[("batch", "B2")]["amount_cents"] == 3
    assert dest[("unallocated", None)]["amount_cents"] == 3
    assert sum(s["amount_cents"] for s in account["splits"]) == 10
    # destinations 顺序：批次按批次号升序，未分配恒在最后
    batch_dests = [d for d in account["destinations"] if d["dest"] == "batch"]
    assert [d["batch_no"] for d in batch_dests] == ["B1", "B2"]
    assert account["destinations"][-1]["dest"] == "unallocated"


def test_idempotent_same_content_order_independent_and_conflict(client):
    _shift_id, accounting_id = _setup_accounting(client)
    base = {
        "device_id": "CNC-01",
        "allocation_no": "AL-IDEM",
        "accounting_ids": [accounting_id],
        "batches": [_batch("B1", "10:00:00", "11:00:00")],
    }
    first = _allocate(client, base, expect=201).json()
    # 同内容重试 → 200，idempotent=true，id 相同
    second = _allocate(client, base, expect=200).json()
    assert second["idempotent"] is True
    assert second["id"] == first["id"]
    assert second["accounts"] == first["accounts"]
    # 等价 UTC 时间仍命中原单
    utc_retry = {
        **base,
        "batches": [
            {
                "batch_no": "B1",
                "start": "2026-10-01T02:00:00Z",
                "end": "2026-10-01T03:00:00+00:00",
            }
        ],
    }
    assert _allocate(client, utc_retry, expect=200).json()["id"] == first["id"]

    # 同内容顺序无关（两账两批次）：构造另一个班次（10/5）的第二份账，
    # 用不同列表顺序提交相同集合——账 id 与批次均乱序，仍判同内容 → 200
    r2 = client.post(
        "/shifts",
        json={
            "device_id": "CNC-01",
            "start": "2026-10-05T08:00:00+08:00",
            "end": "2026-10-05T16:00:00+08:00",
        },
    )
    shift2 = r2.json()["id"]
    client.post(
        "/events",
        json=_event("CNC-01", "P2", 1, "09:00:00", "10:00:00", "failure", day="05"),
    )
    client.post(f"/shifts/{shift2}/settle")
    client.post(
        "/devices/CNC-01/rate-versions",
        json={"entries": [_rate_entry("failure", "00:00:00", "23:59:00", "2", day="05")]},
    )
    acc2 = client.post(
        f"/shifts/{shift2}/loss-accountings", json={"rate_version": 2}
    ).json()["id"]
    multi = {
        "device_id": "CNC-01",
        "allocation_no": "AL-IDEM2",
        "accounting_ids": [acc2, accounting_id],
        "batches": [
            _batch("B2", "09:00:00", "10:00:00", day="05"),
            _batch("B1", "10:00:00", "11:00:00"),
        ],
    }
    m1 = _allocate(client, multi, expect=201).json()
    m2 = _allocate(client, multi, expect=200).json()
    assert m2["id"] == m1["id"] and m2["accounts"] == m1["accounts"]
    # 同一内容集合、列表顺序重排（账 id 与批次均换序）→ 仍判同内容
    multi_reordered = {
        **multi,
        "accounting_ids": [accounting_id, acc2],
        "batches": [
            _batch("B1", "10:00:00", "11:00:00"),
            _batch("B2", "09:00:00", "10:00:00", day="05"),
        ],
    }
    m3 = _allocate(client, multi_reordered, expect=200).json()
    assert m3["id"] == m1["id"]
    # 规范化后返回的批次窗口按批次号升序
    assert [w["batch_no"] for w in m3["batch_windows"]] == ["B1", "B2"]

    # 同单号异内容（换窗口）→ 409 ALLOCATION_CONFLICT
    conflict = {
        **base,
        "batches": [_batch("B1", "10:30:00", "11:00:00")],
    }
    r = _allocate(client, conflict, expect=409)
    assert r.json()["detail"]["code"] == "ALLOCATION_CONFLICT"

    # 冲突后原结果仍在、未被改写
    got = client.get("/loss-cost-allocations/AL-IDEM")
    assert got.status_code == 200
    assert got.json()["accounts"] == first["accounts"]


def test_multiple_accounts_different_shifts_in_one_order(client):
    _, acc1 = _setup_accounting(client)
    # 第二个班次（10/4，避开幂等用例占用的 10/2–10/3）：故障 09:00-10:00，
    # 费率 v2 全天 2 元/分 → 12000 分
    r = client.post(
        "/shifts",
        json={
            "device_id": "CNC-01",
            "start": "2026-10-04T08:00:00+08:00",
            "end": "2026-10-04T16:00:00+08:00",
        },
    )
    shift2 = r.json()["id"]
    client.post(
        "/events",
        json=_event("CNC-01", "P2", 1, "09:00:00", "10:00:00", "failure", day="04"),
    )
    client.post(f"/shifts/{shift2}/settle")
    client.post(
        "/devices/CNC-01/rate-versions",
        json={"entries": [_rate_entry("failure", "00:00:00", "23:59:00", "2", day="04")]},
    )
    acc2 = client.post(
        f"/shifts/{shift2}/loss-accountings", json={"rate_version": 2}
    ).json()["id"]

    body = {
        "device_id": "CNC-01",
        "allocation_no": "AL-MULTI",
        "accounting_ids": [acc2, acc1],  # 乱序
        "batches": [
            _batch("X1", "10:00:00", "12:00:00", day="01"),
            _batch("X2", "09:00:00", "10:00:00", day="04"),
        ],
    }
    data = _allocate(client, body, expect=201).json()
    ids = sorted(a["accounting_id"] for a in data["accounts"])
    assert ids == sorted([acc1, acc2])
    # 每个账各自守恒
    for account in data["accounts"]:
        assert (
            sum(d["amount_cents"] for d in account["destinations"])
            == account["totals"]["amount_cents"]
        )


# ---------------------------------------------------------------- 拒绝类（整单不留痕）


def _alloc_count(client):
    with engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM loss_cost_allocations")).scalar()


def test_unknown_device_rejected(client):
    r = _allocate(
        client,
        {
            "device_id": "NOPE",
            "allocation_no": "X",
            "accounting_ids": [1],
            "batches": [_batch("B", "10:00:00", "11:00:00")],
        },
        device="NOPE",
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_unknown_or_cross_device_accounting_rejected_entirely(client):
    _, acc1 = _setup_accounting(client)
    before = _alloc_count(client)
    # 未知账 id
    r = _allocate(
        client,
        {
            "device_id": "CNC-01",
            "allocation_no": "AL-BAD1",
            "accounting_ids": [acc1, 999999],
            "batches": [_batch("B", "10:00:00", "11:00:00")],
        },
        expect=404,
    )
    assert r.json()["detail"]["code"] == "LOSS_ACCOUNTING_NOT_FOUND"

    # 跨设备账：CNC-02 独立建账（费率版本 1）后混入本设备的单
    client.post("/devices", json={"id": "CNC-02", "name": "二号机"})
    r2 = client.post(
        "/shifts",
        json={
            "device_id": "CNC-02",
            "start": "2026-10-06T08:00:00+08:00",
            "end": "2026-10-06T16:00:00+08:00",
        },
    )
    s2 = r2.json()["id"]
    client.post(
        "/events",
        json=_event("CNC-02", "P", 1, "09:00:00", "10:00:00", "failure", day="06"),
    )
    client.post(f"/shifts/{s2}/settle")
    client.post(
        "/devices/CNC-02/rate-versions",
        json={"entries": [_rate_entry("failure", "00:00:00", "23:59:00", "1", day="06")]},
    )
    other = client.post(
        f"/shifts/{s2}/loss-accountings", json={"rate_version": 1}
    ).json()["id"]
    r = _allocate(
        client,
        {
            "device_id": "CNC-01",
            "allocation_no": "AL-BAD2",
            "accounting_ids": [acc1, other],
            "batches": [_batch("B", "10:00:00", "11:00:00")],
        },
        expect=404,
    )
    assert r.json()["detail"]["code"] == "LOSS_ACCOUNTING_NOT_FOUND"
    assert _alloc_count(client) == before  # 整单不留痕


def test_two_accountings_same_shift_rejected(client):
    _shift_id, acc1 = _setup_accounting(client)
    # 同班次修订 1 × 费率版本 2（新费率）→ 第二份账
    client.post(
        "/devices/CNC-01/rate-versions",
        json={"entries": [_rate_entry("failure", "00:00:00", "23:59:00", "2")]},
    )
    acc2 = client.post(
        f"/shifts/{_shift_id}/loss-accountings", json={"rate_version": 2}
    ).json()["id"]
    r = _allocate(
        client,
        {
            "device_id": "CNC-01",
            "allocation_no": "AL-SAMESHIFT",
            "accounting_ids": [acc1, acc2],
            "batches": [_batch("B", "10:00:00", "11:00:00")],
        },
        expect=422,
    )
    assert r.json()["detail"]["code"] == "ACCOUNTINGS_SAME_SHIFT"
    assert _alloc_count(client) == 0


def test_invalid_batch_windows_rejected_entirely(client):
    _, acc1 = _setup_accounting(client)

    def _code(batches):
        r = _allocate(
            client,
            {
                "device_id": "CNC-01",
                "allocation_no": "AL-BADWIN",
                "accounting_ids": [acc1],
                "batches": batches,
            },
        )
        assert r.status_code in (409, 422), r.text
        return r.status_code, r.json()["detail"]["code"]

    # 窗口重叠
    assert _code(
        [_batch("A", "10:00:00", "11:30:00"), _batch("B", "11:00:00", "12:00:00")]
    ) == (422, "BATCH_WINDOW_INVALID")
    # 批次号重复
    assert _code(
        [_batch("A", "10:00:00", "11:00:00"), _batch("A", "11:00:00", "12:00:00")]
    ) == (422, "BATCH_WINDOW_INVALID")
    # 倒置 / 空窗口
    assert _code([_batch("A", "11:00:00", "11:00:00")])[1] == "VALIDATION_ERROR"
    # 缺时区
    r = client.post(
        "/devices/CNC-01/loss-cost-allocations",
        json={
            "device_id": "CNC-01",
            "allocation_no": "AL-BADWIN",
            "accounting_ids": [acc1],
            "batches": [
                {
                    "batch_no": "A",
                    "start": "2026-10-01T10:00:00",
                    "end": "2026-10-01T11:00:00",
                }
            ],
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
    # 空列表 / 重复账 id → 422
    for bad_body in (
        {
            "device_id": "CNC-01",
            "allocation_no": "AL-BADWIN",
            "accounting_ids": [acc1],
            "batches": [],
        },
        {
            "device_id": "CNC-01",
            "allocation_no": "AL-BADWIN",
            "accounting_ids": [acc1, acc1],
            "batches": [_batch("A", "10:00:00", "11:00:00")],
        },
    ):
        rr = client.post("/devices/CNC-01/loss-cost-allocations", json=bad_body)
        assert rr.status_code == 422, rr.text
    # 所有拒绝后无分摊单残留
    assert _alloc_count(client) == 0


def test_path_device_mismatch_rejected(client):
    _, acc1 = _setup_accounting(client)
    r = client.post(
        "/devices/CNC-02/loss-cost-allocations",
        json={
            "device_id": "CNC-01",
            "allocation_no": "AL-MISMATCH",
            "accounting_ids": [acc1],
            "batches": [_batch("B", "10:00:00", "11:00:00")],
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- 固化与不可变


def test_allocation_history_immutable_after_revision_and_rate_release(client):
    shift_id, acc1 = _setup_accounting(client)
    body = {
        "device_id": "CNC-01",
        "allocation_no": "AL-FROZEN",
        "accounting_ids": [acc1],
        "batches": [_batch("B1", "10:00:00", "12:00:00")],
    }
    first = _allocate(client, body, expect=201).json()

    # 之后：归因更正（故障缩短）产生修订 2，发布新费率版本 2
    client.post(
        "/events",
        json=_event("CNC-01", "PLC-9", 2, "10:00:00", "11:00:00", "failure"),
    )
    client.post(
        "/devices/CNC-01/rate-versions",
        json={"entries": [_rate_entry("failure", "00:00:00", "23:59:00", "5")]},
    )
    # 新组合的新账存在，但旧分摊单内容不变
    again = client.get("/loss-cost-allocations/AL-FROZEN")
    assert again.status_code == 200
    assert again.json()["accounts"] == first["accounts"]
    assert again.json()["idempotent"] is True

    # 设备列表包含该单
    listing = client.get("/devices/CNC-01/loss-cost-allocations")
    assert [x["allocation_no"] for x in listing.json()] == ["AL-FROZEN"]


def test_get_unknown_allocation_404(client):
    r = client.get("/loss-cost-allocations/NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ALLOCATION_NOT_FOUND"


def test_concurrent_same_no_same_content_single_order(client):
    import threading

    _, acc1 = _setup_accounting(client)
    body = {
        "device_id": "CNC-01",
        "allocation_no": "AL-CONC",
        "accounting_ids": [acc1],
        "batches": [_batch("B1", "10:00:00", "11:00:00")],
    }
    barrier = threading.Barrier(4)
    statuses: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=10)
            r = client.post("/devices/CNC-01/loss-cost-allocations", json=body)
            with lock:
                statuses.append(r.status_code)
                assert r.status_code in (200, 201), r.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert statuses.count(201) == 1
    assert statuses.count(200) == 3
    assert _alloc_count(client) == 1
