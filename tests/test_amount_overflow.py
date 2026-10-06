"""金额链路越界（422 AMOUNT_OVERFLOW）端到端测试。

费率本身合法（最多 12 位整数 + 6 位小数，恒在 BIGINT 内）即可发布，
不因"未来可能越界"被拒绝；但长班次逐段核算的段金额/账合计，以及多班次
期间结账的金额汇总可能越过数据库 BIGINT 金额列（2^63 - 1 分）：

- 单账任一切分段、账头或结账汇总越界 → 422 AMOUNT_OVERFLOW（统一错误体
  {"detail": {"code", "message"}}），整笔操作回滚，不留下部分账、明细
  或结账；已固化历史不改写；
- 边界内金额精确保存（不截断、不饱和），同组合核算/同单号结账重试仍
  返回原结果；越界失败的结账不占用单号，修正费率后可同单号重新结账。

运行方式：docker compose exec app pytest tests/test_amount_overflow.py
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "loss_period_closing_items",
    "loss_period_closings",
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
DEVICE_ID = DEVICE["id"]

BASE = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

#: 合法费率上限：12 位整数 + 6 位小数（= 999999999999999999 微元/分钟，
#: 本身恒在 BIGINT 内，发布不被拒绝）
MAX_RATE = "999999999999.999999"
#: BIGINT 金额列上限（分）
MAX_CENTS = 2**63 - 1
#: 92233 分钟 × 上限费率 = 9223299999999999991 分，恰在界内（精确保存）
FIT_MINUTES = 92233
FIT_CENTS = 9223299999999999991
#: 多 1 分钟：段金额 9223399999999999991 分越界
OVER_MINUTES = 92234
#: 50000 分钟 × 上限费率 = 4999999999999999995 分：单笔在界内，两笔合计越界
HALF_MINUTES = 50000
HALF_CENTS = 4999999999999999995


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


# ---------------------------------------------------------------- 构造数据


def _shift(client, start: datetime, minutes: int) -> int:
    r = client.post(
        "/shifts",
        json={
            "device_id": DEVICE_ID,
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=minutes)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _event_covering(client, source: str, start: datetime, minutes: int):
    r = client.post(
        "/events",
        json={
            "device_id": DEVICE_ID,
            "source_id": source,
            "version": 1,
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=minutes)).isoformat(),
            "reason": "failure",
        },
    )
    assert r.status_code == 201, r.text


def _settled_long_shift(client, start: datetime, minutes: int, source: str) -> int:
    """整班被一条故障事件覆盖并已结算的长班次。"""
    shift_id = _shift(client, start, minutes)
    _event_covering(client, source, start, minutes)
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["totals"]["failure"] == minutes
    return shift_id


def _rate(reason, start: datetime, end: datetime, amount: str) -> dict:
    return {
        "reason": reason,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "amount_per_minute": amount,
    }


def _publish(client, entries, expect=201) -> dict:
    r = client.post(f"/devices/{DEVICE_ID}/rate-versions", json={"entries": entries})
    assert r.status_code == expect, r.text
    return r.json()


def _account(client, shift_id: int, rate_version: int):
    return client.post(
        f"/shifts/{shift_id}/loss-accountings",
        json={"rate_version": rate_version},
    )


def _accounting_ids(client, shift_id: int) -> list[int]:
    r = client.get(f"/shifts/{shift_id}/loss-accountings")
    assert r.status_code == 200, r.text
    return [row["id"] for row in r.json()]


def _max_seq(client) -> int:
    r = client.get(
        f"/devices/{DEVICE_ID}/shift-snapshots"
        "?start=2000-01-01T00:00:00%2B00:00&end=2030-01-01T00:00:00%2B00:00&limit=1"
    )
    assert r.status_code == 200, r.text
    return r.json()["snapshot_seq"]


def _close(client, closing_no: str, rate_version: int, seq: int):
    return client.post(
        f"/devices/{DEVICE_ID}/loss-closings",
        json={
            "device_id": DEVICE_ID,
            "closing_no": closing_no,
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2027-01-01T00:00:00+00:00",
            "seq": seq,
            "rate_version": rate_version,
        },
    )


def _assert_amount_overflow(resp) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["detail"]["code"] == "AMOUNT_OVERFLOW"
    assert body["detail"]["message"]


# ---------------------------------------------------------------- 费率发布


def test_max_legal_rate_publishes_and_is_idempotent(client):
    """合法费率（12 位整数 + 6 位小数）正常发布且幂等，不因未来可能越界被拒。"""
    client.post("/devices", json=DEVICE)
    span_end = BASE + timedelta(minutes=OVER_MINUTES)
    first = _publish(client, [_rate("failure", BASE, span_end, MAX_RATE)])
    assert first["version"] == 1 and first["idempotent"] is False
    again = _publish(client, [_rate("failure", BASE, span_end, MAX_RATE)], expect=200)
    assert again["version"] == 1 and again["idempotent"] is True

    # 超出 12 位整数的费率是非法输入（既有规则，与越界无关）
    r = client.post(
        f"/devices/{DEVICE_ID}/rate-versions",
        json={"entries": [_rate("failure", BASE, span_end, "1000000000000")]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "RATE_ENTRY_INVALID"


# ---------------------------------------------------------------- 单账越界


def test_piece_amount_at_bound_accounted_exactly(client):
    """边界内（合计恰小于 2^63 分）：精确保存，不截断、不饱和；重试幂等。"""
    client.post("/devices", json=DEVICE)
    shift_id = _settled_long_shift(client, BASE, FIT_MINUTES, "PLC-1")
    _publish(client, [_rate("failure", BASE, BASE + timedelta(minutes=FIT_MINUTES), MAX_RATE)])

    r = _account(client, shift_id, 1)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["totals"]["amount_cents"] == FIT_CENTS
    assert data["totals"]["amount"] == "92232999999999999.91"
    assert data["totals"]["priced_minutes"] == FIT_MINUTES
    assert len(data["priced_pieces"]) == 1
    assert data["priced_pieces"][0]["amount_cents"] == FIT_CENTS
    assert data["priced_pieces"][0]["rate_per_minute"] == MAX_RATE

    # 同组合重试 → 200 幂等，内容一致
    r2 = _account(client, shift_id, 1)
    assert r2.status_code == 200 and r2.json()["idempotent"] is True
    assert r2.json()["totals"]["amount_cents"] == FIT_CENTS

    # 大额账可原样读回（历史不可改写）
    rows = client.get(f"/shifts/{shift_id}/loss-accountings").json()
    assert len(rows) == 1 and rows[0]["totals"]["amount_cents"] == FIT_CENTS


def test_piece_amount_overflow_rolls_back(client):
    """单段金额越界 → 422 AMOUNT_OVERFLOW；不留部分账，重试结果一致。"""
    client.post("/devices", json=DEVICE)
    shift_id = _settled_long_shift(client, BASE, OVER_MINUTES, "PLC-1")
    _publish(client, [_rate("failure", BASE, BASE + timedelta(minutes=OVER_MINUTES), MAX_RATE)])

    _assert_amount_overflow(_account(client, shift_id, 1))
    # 不留下部分账或明细
    assert _accounting_ids(client, shift_id) == []
    # 同组合重试：仍是同一越界结果（无部分状态、确定性拒绝）
    _assert_amount_overflow(_account(client, shift_id, 1))
    assert _accounting_ids(client, shift_id) == []
    # 班次结算结果（已固化历史）不受影响
    result = client.get(f"/shifts/{shift_id}/result")
    assert result.status_code == 200
    assert result.json()["totals"]["failure"] == OVER_MINUTES


def test_total_overflow_across_rate_boundaries_rolls_back(client):
    """逐段均在界内、账头合计越界 → 422 AMOUNT_OVERFLOW，整笔回滚。"""
    client.post("/devices", json=DEVICE)
    minutes = 2 * HALF_MINUTES
    shift_id = _settled_long_shift(client, BASE, minutes, "PLC-1")
    mid = BASE + timedelta(minutes=HALF_MINUTES)
    end = BASE + timedelta(minutes=minutes)
    _publish(
        client,
        [
            _rate("failure", BASE, mid, MAX_RATE),
            _rate("failure", mid, end, MAX_RATE),  # 端点相接，合法
        ],
    )

    _assert_amount_overflow(_account(client, shift_id, 1))
    assert _accounting_ids(client, shift_id) == []


# ---------------------------------------------------------------- 结账汇总越界


def test_closing_aggregate_overflow_rolls_back(client):
    """多班次结账汇总越界 → 422；整单回滚，既有账不改写，单号不占用。"""
    client.post("/devices", json=DEVICE)
    start_b = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    s1 = _settled_long_shift(client, BASE, HALF_MINUTES, "PLC-1")
    s2 = _settled_long_shift(client, start_b, HALF_MINUTES, "PLC-2")
    span_end = start_b + timedelta(minutes=HALF_MINUTES)
    _publish(client, [_rate("failure", BASE, span_end, MAX_RATE)])

    # 两班各自核算均在界内（各 4999999999999999995 分）
    for sid in (s1, s2):
        r = _account(client, sid, 1)
        assert r.status_code == 201, r.text
        assert r.json()["totals"]["amount_cents"] == HALF_CENTS

    # 结账汇总 9999999999999999990 分 > 2^63 - 1 → 422 AMOUNT_OVERFLOW
    _assert_amount_overflow(_close(client, "LC-OVER", 1, _max_seq(client)))
    # 不留结账记录
    missing = client.get("/loss-closings/LC-OVER")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "CLOSING_NOT_FOUND"
    # 已固化的两份账原样保留（历史不改写）
    for sid in (s1, s2):
        rows = client.get(f"/shifts/{sid}/loss-accountings").json()
        assert len(rows) == 1
        assert rows[0]["totals"]["amount_cents"] == HALF_CENTS
    # 同单号同输入重试：仍是同一越界结果（未留下任何部分状态）
    _assert_amount_overflow(_close(client, "LC-OVER", 1, _max_seq(client)))

    # 失败的结账不占用单号：发布更低费率后同单号重新结账成功
    _publish(client, [_rate("failure", BASE, span_end, "1")])
    ok = _close(client, "LC-OVER", 2, _max_seq(client))
    assert ok.status_code == 201, ok.text
    assert ok.json()["totals"]["amount_cents"] == 2 * HALF_MINUTES * 100
    # 成功的结账同单号同输入重试 → 200 返回原结果
    again = _close(client, "LC-OVER", 2, _max_seq(client))
    assert again.status_code == 200 and again.json()["idempotent"] is True
    assert again.json()["totals"]["amount_cents"] == 2 * HALF_MINUTES * 100


def test_closing_forming_accountings_rolls_back_atomically(client):
    """结账同事务新形成的账随汇总越界一并回滚：不留部分账、明细或结账。"""
    client.post("/devices", json=DEVICE)
    start_b = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    s1 = _settled_long_shift(client, BASE, HALF_MINUTES, "PLC-1")
    s2 = _settled_long_shift(client, start_b, HALF_MINUTES, "PLC-2")
    span_end = start_b + timedelta(minutes=HALF_MINUTES)
    _publish(client, [_rate("failure", BASE, span_end, MAX_RATE)])

    # 事先不单独核算：结账事务内逐班形成账，汇总越界 → 整单回滚
    _assert_amount_overflow(_close(client, "LC-ATOMIC", 1, _max_seq(client)))
    assert _accounting_ids(client, s1) == []
    assert _accounting_ids(client, s2) == []
    assert client.get("/loss-closings/LC-ATOMIC").status_code == 404


def test_closing_piece_level_overflow_rolls_back(client):
    """结账事务内形成账时段金额越界 → 422；不留账、不留结账。"""
    client.post("/devices", json=DEVICE)
    shift_id = _settled_long_shift(client, BASE, OVER_MINUTES, "PLC-1")
    _publish(client, [_rate("failure", BASE, BASE + timedelta(minutes=OVER_MINUTES), MAX_RATE)])

    _assert_amount_overflow(_close(client, "LC-PIECE", 1, _max_seq(client)))
    assert _accounting_ids(client, shift_id) == []
    assert client.get("/loss-closings/LC-PIECE").status_code == 404


# ---------------------------------------------------------------- 大额边界值流经分摊


def test_allocation_of_boundary_amount_conserves_total(client):
    """边界内大额账按批次分摊：各去向金额之和恒等于原账金额（分毫不差）。"""
    client.post("/devices", json=DEVICE)
    shift_id = _settled_long_shift(client, BASE, FIT_MINUTES, "PLC-1")
    _publish(client, [_rate("failure", BASE, BASE + timedelta(minutes=FIT_MINUTES), MAX_RATE)])
    accounting_id = _account(client, shift_id, 1).json()["id"]

    r = client.post(
        f"/devices/{DEVICE_ID}/loss-cost-allocations",
        json={
            "device_id": DEVICE_ID,
            "allocation_no": "AL-BOUND",
            "accounting_ids": [accounting_id],
            "batches": [
                {
                    "batch_no": "B1",
                    "start": BASE.isoformat(),
                    "end": (BASE + timedelta(minutes=60)).isoformat(),
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    account = r.json()["accounts"][0]
    assert account["totals"]["amount_cents"] == FIT_CENTS
    splits = account["splits"]
    assert sum(sp["amount_cents"] for sp in splits) == FIT_CENTS
    assert all(0 <= sp["amount_cents"] <= FIT_CENTS for sp in splits)
    by_dest = {sp["dest"] for sp in splits}
    assert by_dest == {"batch", "unallocated"}
