"""金额链路 BIGINT 越界防护端到端测试（需 PostgreSQL）。

覆盖本轮修复：
- 合法费率（每分钟金额最多 12 位整数）始终可发布，不因"未来可能越界"被拒绝；
- 长班次逐段核算：任一定价明细金额或账头合计越界 → 422 AMOUNT_OVERFLOW
  （错误体沿用 {"detail":{"code","message"}}），整笔回滚，不留账头/明细；
- 多班次期间结账：汇总越界 → 422 AMOUNT_OVERFLOW，结账头、条目及事务内
  新建损失账全部回滚；已固化历史不被改写；
- 边界内金额精确保存（不截断、不饱和），待定价分钟无金额、永不越界；
- 同组合核算 / 同单号结账重试返回原结果（幂等语义不因本修复改变）。

运行方式：docker compose exec app pytest tests/test_amount_overflow.py
"""

from datetime import datetime, timedelta

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

DEVICE = {"id": "CNC-BIG", "name": "大额链路机台"}
# 每分钟 999,999,999,999 元（12 位整数上限，合法）
MAX_RATE = "999999999999"


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


# ---------------------------------------------------------------- 构造数据


def _long_shift(client, idx: int, minutes: int, start_iso: str) -> int:
    """创建一个已结算的长班次：故障贯穿整班（start 起 minutes 分钟）。

    班次/事件/结算后固化为单段 failure [start, end)。
    """
    s = datetime.fromisoformat(start_iso)
    e = s + timedelta(minutes=minutes)
    client.post("/devices", json=DEVICE)  # 重复注册幂等
    r = client.post(
        "/shifts",
        json={
            "device_id": DEVICE["id"],
            "start": s.isoformat(),
            "end": e.isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    shift_id = r.json()["id"]
    er = client.post(
        "/events",
        json={
            "device_id": DEVICE["id"],
            "source_id": f"PLC-{idx}",
            "version": 1,
            "start": s.isoformat(),
            "end": e.isoformat(),
            "reason": "failure",
        },
    )
    assert er.status_code == 201, er.text
    sr = client.post(f"/shifts/{shift_id}/settle")
    assert sr.status_code == 200, sr.text
    return shift_id


def _publish_max_rate(client, *, sh="2026-01-01T00:00:00+08:00", eh="2027-12-31T00:00:00+08:00"):
    r = client.post(
        f"/devices/{DEVICE['id']}/rate-versions",
        json={
            "entries": [
                {
                    "reason": "failure",
                    "start": sh,
                    "end": eh,
                    "amount_per_minute": MAX_RATE,
                }
            ]
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["version"]


def _count(sql: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


# ---------------------------------------------------------------- 费率发布


def test_max_rate_is_publishable_despite_possible_overflow(client):
    """合法的 12 位整数每分钟金额必须可发布——不因未来长班次可能越界被拒。"""
    client.post("/devices", json=DEVICE)
    r = client.post(
        f"/devices/{DEVICE['id']}/rate-versions",
        json={
            "entries": [
                {
                    "reason": "failure",
                    "start": "2026-01-01T00:00:00+08:00",
                    "end": "2026-12-31T00:00:00+08:00",
                    "amount_per_minute": MAX_RATE,
                }
            ]
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1
    # 同表重试幂等返回原版本（合法费率不被拒，幂等语义不变）
    r2 = client.post(
        f"/devices/{DEVICE['id']}/rate-versions",
        json={
            "entries": [
                {
                    "reason": "failure",
                    "start": "2026-01-01T00:00:00+08:00",
                    "end": "2026-12-31T00:00:00+08:00",
                    "amount_per_minute": "999999999999.000000",
                }
            ]
        },
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotent"] is True


def test_13_integer_digit_rate_still_rejected_as_invalid(client):
    """费率发布侧既有校验不变：13 位整数分钟金额仍是 RATE_ENTRY_INVALID。"""
    client.post("/devices", json=DEVICE)
    r = client.post(
        f"/devices/{DEVICE['id']}/rate-versions",
        json={
            "entries": [
                {
                    "reason": "failure",
                    "start": "2026-01-01T00:00:00+08:00",
                    "end": "2026-12-31T00:00:00+08:00",
                    "amount_per_minute": "1000000000000",  # 10^12，13 位
                }
            ]
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "RATE_ENTRY_INVALID"


# ---------------------------------------------------------------- 核算越界


def test_accounting_piece_overflow_returns_422_and_rolls_back(client):
    # 100,000 分钟（约 69.4 天）× 999,999,999,999 元/分 ≈ 9.9999×10^18 分，
    # 超过 BIGINT 上界 2^63-1 ≈ 9.22×10^18 分。
    shift_id = _long_shift(client, 1, minutes=100_000, start_iso="2026-10-01T00:00:00+08:00")
    version = _publish_max_rate(client)

    r = client.post(
        f"/shifts/{shift_id}/loss-accountings",
        json={"rate_version": version},
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "AMOUNT_OVERFLOW"
    assert detail["message"]

    # 整笔回滚：不留账头，也不留任何切分明细
    assert _count(
        f"SELECT COUNT(*) FROM loss_accountings WHERE shift_id = {shift_id}"
    ) == 0
    assert _count("SELECT COUNT(*) FROM loss_accounting_pieces") == 0
    # 既有费率版本与结算历史原样保留（已固化历史不改写）
    assert _count("SELECT COUNT(*) FROM rate_versions") == 1
    assert _count("SELECT COUNT(*) FROM shift_results") == 1


def test_accounting_overflow_retry_keeps_returning_422_without_rows(client):
    """同组合重试不改变结果：每次都是 422，且始终不留下部分账。"""
    shift_id = _long_shift(client, 1, minutes=100_000, start_iso="2026-10-01T00:00:00+08:00")
    version = _publish_max_rate(client)
    for _ in range(2):
        r = client.post(
            f"/shifts/{shift_id}/loss-accountings",
            json={"rate_version": version},
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "AMOUNT_OVERFLOW"
    assert _count("SELECT COUNT(*) FROM loss_accountings") == 0
    assert _count("SELECT COUNT(*) FROM loss_accounting_pieces") == 0


def test_in_bounds_large_amount_is_stored_exactly(client):
    """边界内大额精确保存：100,000 分钟 × 100,000,000,000 元/分
    = 10^18 分（< 2^63-1），不截断、不饱和，账面与明细逐字节一致。"""
    shift_id = _long_shift(client, 1, minutes=100_000, start_iso="2026-10-01T00:00:00+08:00")
    client.post("/devices", json=DEVICE)
    pr = client.post(
        f"/devices/{DEVICE['id']}/rate-versions",
        json={
            "entries": [
                {
                    "reason": "failure",
                    "start": "2026-01-01T00:00:00+08:00",
                    "end": "2027-12-31T00:00:00+08:00",
                    "amount_per_minute": "100000000000",  # 10^11 元/分
                }
            ]
        },
    )
    assert pr.status_code == 201, pr.text
    version = pr.json()["version"]

    r = client.post(
        f"/shifts/{shift_id}/loss-accountings",
        json={"rate_version": version},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["totals"]["amount_cents"] == 10**18
    assert body["totals"]["amount"] == f"{10**16}.00"
    assert len(body["priced_pieces"]) == 1
    assert body["priced_pieces"][0]["amount_cents"] == 10**18

    # 同组合再核算 → 返回原账（200，幂等），金额不变
    r2 = client.post(
        f"/shifts/{shift_id}/loss-accountings",
        json={"rate_version": version},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotent"] is True
    assert r2.json()["totals"]["amount_cents"] == 10**18


def test_long_unpriced_minutes_have_no_amount_and_do_not_overflow(client):
    """待定价分钟无金额：百万分钟完全无费率覆盖时可正常固化空金额账。"""
    shift_id = _long_shift(client, 1, minutes=1_000_000, start_iso="2026-10-01T00:00:00+08:00")
    # 费率版本只覆盖一个与班次不相交的窗口 → 全部分钟待定价
    pr = client.post(
        f"/devices/{DEVICE['id']}/rate-versions",
        json={
            "entries": [
                {
                    "reason": "failure",
                    "start": "2030-01-01T00:00:00+08:00",
                    "end": "2030-01-02T00:00:00+08:00",
                    "amount_per_minute": MAX_RATE,
                }
            ]
        },
    )
    assert pr.status_code == 201, pr.text
    r = client.post(
        f"/shifts/{shift_id}/loss-accountings",
        json={"rate_version": pr.json()["version"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["totals"]["amount_cents"] == 0
    assert body["totals"]["unpriced_minutes"] == 1_000_000
    assert body["priced_pieces"] == []
    assert len(body["unpriced_pieces"]) == 1
    assert body["unpriced_pieces"][0]["minutes"] == 1_000_000


# ---------------------------------------------------------------- 结账越界


def test_closing_total_overflow_returns_422_and_rolls_back_everything(client):
    # 两个互不重叠的长班次各 60,000 分钟（约 41.7 天，班次 10/01、12/01 起）：
    # 每份账 ≈ 5.99999×10^18 分，单独都在 BIGINT 界内；
    # 结账机械求和 ≈ 1.19999×10^19 分 > 2^63-1 → 整单汇总越界。
    s1 = _long_shift(client, 1, minutes=60_000, start_iso="2026-10-01T00:00:00+08:00")
    s2 = _long_shift(client, 2, minutes=60_000, start_iso="2026-12-01T00:00:00+08:00")
    version = _publish_max_rate(client)
    max_seq = 2  # 两个班次各结算一次，seq=1、2

    body = {
        "device_id": DEVICE["id"],
        "closing_no": "LC-OVERFLOW",
        "start": "2026-09-01T00:00:00+08:00",
        "end": "2027-03-01T00:00:00+08:00",
        "seq": max_seq,
        "rate_version": version,
    }
    r = client.post(f"/devices/{DEVICE['id']}/loss-closings", json=body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "AMOUNT_OVERFLOW"

    # 整单回滚：无结账头、无条目
    assert _count("SELECT COUNT(*) FROM loss_period_closings") == 0
    assert _count("SELECT COUNT(*) FROM loss_period_closing_items") == 0
    # 结账事务内为两个班次新建的损失账也随回滚撤销（不留孤儿账/明细）
    assert _count("SELECT COUNT(*) FROM loss_accountings") == 0
    assert _count("SELECT COUNT(*) FROM loss_accounting_pieces") == 0
    # 按单号读取 → 404（同单号重试不会命中"半成品"）
    assert client.get("/loss-closings/LC-OVERFLOW").status_code == 404


def test_closing_overflow_retry_is_stable_422(client):
    s1 = _long_shift(client, 1, minutes=60_000, start_iso="2026-10-01T00:00:00+08:00")
    s2 = _long_shift(client, 2, minutes=60_000, start_iso="2026-12-01T00:00:00+08:00")
    version = _publish_max_rate(client)
    body = {
        "device_id": DEVICE["id"],
        "closing_no": "LC-OVR-RETRY",
        "start": "2026-09-01T00:00:00+08:00",
        "end": "2027-03-01T00:00:00+08:00",
        "seq": 2,
        "rate_version": version,
    }
    for _ in range(2):
        r = client.post(f"/devices/{DEVICE['id']}/loss-closings", json=body)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "AMOUNT_OVERFLOW"
    assert _count("SELECT COUNT(*) FROM loss_period_closings") == 0
    assert _count("SELECT COUNT(*) FROM loss_accountings") == 0


def test_closing_overflow_does_not_touch_existing_accounts_or_closings(client):
    """结账越界回滚时，此前已固化的历史账与历史结账原样保留、不改写。"""
    # 班次 1：1 分钟短班（2026-01，费率覆盖期内），账在界内、提前单独固化
    short = _long_shift(client, 9, minutes=1, start_iso="2026-01-15T00:00:00+08:00")
    version = _publish_max_rate(client)
    ra = client.post(
        f"/shifts/{short}/loss-accountings", json={"rate_version": version}
    )
    assert ra.status_code == 201, ra.text
    saved_total = ra.json()["totals"]["amount_cents"]

    # 一张历史空结账也已固化
    ok_body = {
        "device_id": DEVICE["id"],
        "closing_no": "LC-HIST",
        "start": "2020-01-01T00:00:00+08:00",
        "end": "2020-02-01T00:00:00+08:00",
        "seq": 0,
        "rate_version": version,
    }
    rc = client.post(f"/devices/{DEVICE['id']}/loss-closings", json=ok_body)
    assert rc.status_code == 201, rc.text

    # 两个互不重叠的长班次（各约 41.7 天，单份账在界内、合计越界）
    _long_shift(client, 1, minutes=60_000, start_iso="2026-10-01T00:00:00+08:00")
    _long_shift(client, 2, minutes=60_000, start_iso="2026-12-01T00:00:00+08:00")
    bad = {
        "device_id": DEVICE["id"],
        "closing_no": "LC-OVERFLOW2",
        "start": "2026-09-01T00:00:00+08:00",
        "end": "2027-03-01T00:00:00+08:00",
        "seq": 3,
        "rate_version": version,
    }
    r = client.post(f"/devices/{DEVICE['id']}/loss-closings", json=bad)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "AMOUNT_OVERFLOW"

    # 失败结账不留痕
    assert client.get("/loss-closings/LC-OVERFLOW2").status_code == 404
    # 历史账原样可读、金额不变
    rg = client.get(f"/shifts/{short}/loss-accountings")
    assert rg.status_code == 200
    assert rg.json()[0]["totals"]["amount_cents"] == saved_total
    # 历史结账原样可读；同单号同输入重试仍返回原结账
    rh = client.get("/loss-closings/LC-HIST")
    assert rh.status_code == 200
    rh2 = client.post(f"/devices/{DEVICE['id']}/loss-closings", json=ok_body)
    assert rh2.status_code == 200, rh2.text
    assert rh2.json()["idempotent"] is True


def test_closing_with_in_bounds_accounts_succeeds_and_is_idempotent(client):
    """对照：短班次结账在界内成功固化；同单号同输入重试返回原结账。"""
    s1 = _long_shift(client, 1, minutes=1, start_iso="2026-10-01T00:00:00+08:00")
    s2 = _long_shift(client, 2, minutes=1, start_iso="2026-11-01T00:00:00+08:00")
    version = _publish_max_rate(client)
    body = {
        "device_id": DEVICE["id"],
        "closing_no": "LC-OK",
        "start": "2026-09-01T00:00:00+08:00",
        "end": "2027-01-01T00:00:00+08:00",
        "seq": 2,
        "rate_version": version,
    }
    r = client.post(f"/devices/{DEVICE['id']}/loss-closings", json=body)
    assert r.status_code == 201, r.text
    # 2 分钟 × 999,999,999,999 元/分，每段按分四舍五入后各 10^14 分
    assert r.json()["totals"]["amount_cents"] == 2 * 10**14
    r2 = client.post(f"/devices/{DEVICE['id']}/loss-closings", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotent"] is True
    assert r2.json()["totals"]["amount_cents"] == 2 * 10**14
    # 复用既有账：没有新建第二份
    assert _count("SELECT COUNT(*) FROM loss_accountings") == 2
