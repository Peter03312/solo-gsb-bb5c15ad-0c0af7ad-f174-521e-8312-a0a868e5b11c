"""按设备期间停机损失结账（POST /devices/{id}/loss-closings）端到端测试。

覆盖：
- 期间内起始班次纳入、期间外（起点不在 [start,end)）不纳入；按截止序号选最后修订；
- 整班次用固化归因段按费率版本计价（形成或复用损失账），按原因汇总金额/定价/待定价分钟；
- 整单同事务固化所选修订、修订序号与账引用；
- 无符合班次固化空结账；
- 同单号同输入重试幂等（200），同单号异输入 409 CLOSING_CONFLICT；
- 序号越界 422 INVALID_SEQ、未知设备 404、未知费率版本 404、非法期间 422；
- 任一修订缺少固化明细 → 422 SEGMENTS_UNAVAILABLE 整单失败，不留部分新账/结账记录；
- 复用既有损失账（同组合不新建）；
- 结账后事件更正/费率发布/重新核算不改变历史结账；
- 按单号读取 404、按设备列出。

运行方式：docker compose exec app pytest tests/test_loss_period_closing.py
"""

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


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


# ---------------------------------------------------------------- 构造数据


def _shift(client, day, sh="08:00:00", eh="16:00:00", device="CNC-01"):
    r = client.post(
        "/shifts",
        json={
            "device_id": device,
            "start": f"2026-10-{day}T{sh}+08:00",
            "end": f"2026-10-{day}T{eh}+08:00",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _event(client, source, version, sh, eh, reason, day="01"):
    r = client.post(
        "/events",
        json={
            "device_id": "CNC-01",
            "source_id": source,
            "version": version,
            "start": f"2026-10-{day}T{sh}+08:00",
            "end": f"2026-10-{day}T{eh}+08:00",
            "reason": reason,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r


def _rate_entry(reason, sh, eh, amount, day_start="01", day_end="01"):
    return {
        "reason": reason,
        "start": f"2026-10-{day_start}T{sh}+08:00",
        "end": f"2026-10-{day_end}T{eh}+08:00",
        "amount_per_minute": amount,
    }


def _publish_rates(client, entries, version=None):
    r = client.post("/devices/CNC-01/rate-versions", json={"entries": entries})
    assert r.status_code == 201, r.text
    if version is not None:
        assert r.json()["version"] == version
    return r.json()["version"]


def _close(client, body, device="CNC-01"):
    return client.post(f"/devices/{device}/loss-closings", json=body)


def _max_seq(client):
    """当前已提交的最大变更序号（追平 /changes 后的空页回传原游标）。"""
    # 用足够大的游标翻到末页不合法；直接读最新修订序号：经快照接口的
    # snapshot_seq（缺省固定为当前最大序号）获取
    r = client.get(
        "/devices/CNC-01/shift-snapshots"
        "?start=2000-01-01T00:00:00%2B08:00&end=2030-01-01T00:00:00%2B08:00&limit=1"
    )
    return r.json()["snapshot_seq"]


def _by_reason(resp, *reason):
    rows = {r["reason"]: r for r in resp["totals"]["by_reason"]}
    return tuple(rows[r] for r in reason)


# ---------------------------------------------------------------- 主流程


def test_closing_full_flow(client):
    client.post("/devices", json=DEVICE)
    # 班次 1（10/1）：故障 10:00-12:00（120 分钟）
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "12:00:00", "failure")
    client.post(f"/shifts/{s1}/settle")
    # 班次 2（10/2）：保养 09:00-10:00（60 分钟）
    s2 = _shift(client, "02")
    _event(client, "MES-1", 1, "09:00:00", "10:00:00", "maintenance", day="02")
    client.post(f"/shifts/{s2}/settle")
    # 班次 3（10/3）：故障，暂不结算 → 不应纳入
    s3 = _shift(client, "03")
    _event(client, "PLC-10", 1, "09:00:00", "10:00:00", "failure", day="03")

    version = _publish_rates(
        client,
        [
            _rate_entry("failure", "00:00:00", "23:59:00", "1", "01", "03"),
            _rate_entry("maintenance", "00:00:00", "23:59:00", "2", "01", "03"),
        ],
        version=1,
    )

    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-1",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-03T00:00:00+08:00",
        "seq": 2,  # 班次 1=seq1、班次 2=seq2
        "rate_version": version,
    }
    r = _close(client, body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["closing_no"] == "LC-1"
    assert data["seq"] == 2 and data["rate_version"] == 1
    assert data["idempotent"] is False
    assert [it["shift_id"] for it in data["items"]] == [s1, s2]
    # 故障 120 × 1 = 120.00；保养 60 × 2 = 120.00
    assert data["totals"]["amount"] == "240.00"
    assert data["totals"]["amount_cents"] == 24000
    assert data["totals"]["priced_minutes"] == 180
    assert data["totals"]["unpriced_minutes"] == 0
    m, f, p = _by_reason(data, "maintenance", "failure", "pending")
    assert f == {
        "reason": "failure",
        "priced_minutes": 120,
        "unpriced_minutes": 0,
        "amount": "120.00",
        "amount_cents": 12000,
    }
    assert m["amount_cents"] == 12000 and m["priced_minutes"] == 60
    assert p["amount_cents"] == 0

    # 条目携带所选修订与账引用；班次区间回显
    it1, it2 = data["items"]
    assert it1["revision"] == 1 and it1["revision_seq"] == 1
    assert it2["revision"] == 1 and it2["revision_seq"] == 2
    assert it1["totals"]["amount"] == "120.00"
    assert it2["totals"]["amount"] == "120.00"
    assert it1["shift_start"].endswith("T00:00:00Z")  # 08:00+08 → UTC
    # 明细被账化：班次 1 故障 120 分钟
    assert len(it1["priced_pieces"]) == 1
    assert it1["priced_pieces"][0]["minutes"] == 120
    assert it1["priced_pieces"][0]["rate_per_minute"] == "1"
    assert it1["unpriced_pieces"] == []

    # 未结算班次 3 未纳入
    assert s3 not in [it["shift_id"] for it in data["items"]]


def test_closing_idempotent_same_input_and_conflict(client):
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    client.post(f"/shifts/{s1}/settle")
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)

    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-IDEM",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }
    r1 = _close(client, body)
    assert r1.status_code == 201
    first_id = r1.json()["id"]
    first_accounting = r1.json()["items"][0]["accounting_id"]

    r2 = _close(
        client,
        {
            **body,
            "start": "2026-09-30T16:00:00Z",
            "end": "2026-10-01T16:00:00+00:00",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["idempotent"] is True
    assert r2.json()["id"] == first_id
    assert r2.json()["items"][0]["accounting_id"] == first_accounting

    # 异输入（改期间终点）→ 409 CLOSING_CONFLICT
    changed = {**body, "end": "2026-10-03T00:00:00+08:00"}
    r3 = _close(client, changed)
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "CLOSING_CONFLICT"


def test_empty_closing_when_no_matching_shift(client):
    client.post("/devices", json=DEVICE)
    # 只有未结算班次
    _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)

    # seq=0：尚无任何结算 → 空结账
    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-EMPTY1",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 0,
        "rate_version": 1,
    }
    r = _close(client, body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["items"] == []
    assert data["totals"]["amount"] == "0.00"
    assert data["totals"]["priced_minutes"] == 0
    assert data["totals"]["unpriced_minutes"] == 0
    for row in data["totals"]["by_reason"]:
        assert row["amount_cents"] == 0

    # 期间内无班次（窗口落在无班次日期），seq 有效 → 同样空结账
    s1 = 1  # 未结算班次不影响
    body2 = {
        "device_id": "CNC-01",
        "closing_no": "LC-EMPTY2",
        "start": "2026-11-01T00:00:00+08:00",
        "end": "2026-11-02T00:00:00+08:00",
        "seq": 0,
        "rate_version": 1,
    }
    r2 = _close(client, body2)
    assert r2.status_code == 201
    assert r2.json()["items"] == []
    assert s1  # 消除未用变量提示


def test_period_uses_shift_start_only_half_open(client):
    """只按班次起点是否落在 [start,end) 纳入；端点相接不纳入。"""
    client.post("/devices", json=DEVICE)
    # 班次 1：10/1 08:00-16:00（+08），事件 10:00-11:00
    s_before = _shift(client, "01", "08:00:00", "16:00:00")
    _event(client, "A", 1, "10:00:00", "11:00:00", "failure", day="01")
    client.post(f"/shifts/{s_before}/settle")
    # 班次 2：起点恰在期间终点 10/2 00:00（+08），与上班次端点相接
    s_edge = _shift(client, "02", "00:00:00", "08:00:00")
    _event(client, "B", 1, "02:00:00", "03:00:00", "failure", day="02")
    client.post(f"/shifts/{s_edge}/settle")
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1", "01", "03")], 1)

    # 当前最大序号（班次 2 的修订序号）
    max_seq = _max_seq(client)
    edge_rev = client.get(f"/shifts/{s_edge}/result").json()["revision"]
    assert edge_rev == 1

    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-EDGE",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": max_seq,
        "rate_version": 1,
    }
    r = _close(client, body)
    assert r.status_code == 201, r.text
    ids = [it["shift_id"] for it in r.json()["items"]]
    assert ids == [s_before]  # 起点恰在 end 的班次不纳入

    # 起点恰在 start 上的班次纳入：10/3 00:00 起点的班次
    s_at_start = _shift(client, "03", "00:00:00", "02:00:00")
    _event(client, "C", 1, "00:30:00", "01:00:00", "failure", day="03")
    client.post(f"/shifts/{s_at_start}/settle")
    max_seq2 = _max_seq(client)
    body2 = {
        "device_id": "CNC-01",
        "closing_no": "LC-EDGE2",
        "start": "2026-10-03T00:00:00+08:00",
        "end": "2026-10-04T00:00:00+08:00",
        "seq": max_seq2,
        "rate_version": 1,
    }
    r2 = _close(client, body2)
    assert [it["shift_id"] for it in r2.json()["items"]] == [s_at_start]


def test_closing_pins_last_revision_before_seq(client):
    """截止序号决定所选修订；用 seq 取历史修订，不受之后更正影响。"""
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "12:00:00", "failure")  # 120 分钟
    client.post(f"/shifts/{s1}/settle")  # seq=1 rev1
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)

    # 用 seq=1 结账：选修订 1（故障 120 分钟）
    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-PIN",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }
    r = _close(client, body)
    assert r.status_code == 201
    item = r.json()["items"][0]
    assert item["revision"] == 1
    assert item["totals"]["priced_minutes"] == 120
    pinned_accounting = item["accounting_id"]

    # 之后更正：故障缩短为 60 分钟 → 修订 2（seq=2）
    _event(client, "PLC-9", 2, "10:00:00", "11:00:00", "failure")

    # 历史结账不变（读回仍为修订 1、120 分钟、同一账引用）
    rget = client.get("/loss-closings/LC-PIN")
    assert rget.status_code == 200
    got = rget.json()
    assert got["seq"] == 1
    assert got["items"][0]["revision"] == 1
    assert got["items"][0]["totals"]["priced_minutes"] == 120
    assert got["items"][0]["accounting_id"] == pinned_accounting

    # 用 seq=2 重新结账（新单号）：选修订 2（60 分钟，新组合新账）
    body2 = {**body, "closing_no": "LC-PIN2", "seq": 2}
    r2 = _close(client, body2)
    assert r2.status_code == 201
    item2 = r2.json()["items"][0]
    assert item2["revision"] == 2 and item2["revision_seq"] == 2
    assert item2["totals"]["priced_minutes"] == 60
    assert item2["accounting_id"] != pinned_accounting


def test_closing_reuses_existing_accounting(client):
    """同 (班次, 修订, 费率版本) 账已存在时结账复用，不新建账。"""
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    client.post(f"/shifts/{s1}/settle")
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)

    r = client.post(f"/shifts/{s1}/loss-accountings", json={"rate_version": 1})
    assert r.status_code == 201
    accounting_id = r.json()["id"]

    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-REUSE",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }
    rc = _close(client, body)
    assert rc.status_code == 201
    assert rc.json()["items"][0]["accounting_id"] == accounting_id

    # 账列表中仍只有一份（没有为结账重复建账）
    rl = client.get(f"/shifts/{s1}/loss-accountings")
    assert len(rl.json()) == 1


def test_closing_unpriced_minutes_tracked(client):
    """费率缺口中的分钟单列待定价，整单按原因汇总待定价分钟、不计金额。"""
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    _event(client, "MES-1", 1, "11:00:00", "11:30:00", "maintenance")
    client.post(f"/shifts/{s1}/settle")
    # 只发布故障费率（保养缺费率 → 保养 30 分钟待定价）
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)

    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-UNPRICED",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }
    r = _close(client, body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["totals"]["amount"] == "60.00"  # 仅故障 60 分钟
    assert data["totals"]["priced_minutes"] == 60
    assert data["totals"]["unpriced_minutes"] == 30
    m, f, _p = _by_reason(data, "maintenance", "failure", "pending")
    assert m["unpriced_minutes"] == 30 and m["amount_cents"] == 0
    assert f["priced_minutes"] == 60 and f["amount_cents"] == 6000
    item = data["items"][0]
    assert len(item["unpriced_pieces"]) == 1
    assert item["unpriced_pieces"][0]["reason"] == "maintenance"
    assert item["unpriced_pieces"][0]["minutes"] == 30


def test_closing_immutable_after_new_rates_and_reaccounting(client):
    """结账后发布新费率版本、重新核算，历史结账原样不变。"""
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    client.post(f"/shifts/{s1}/settle")
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)
    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-IMMUT",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }
    r = _close(client, body)
    assert r.status_code == 201
    before = r.json()

    # 发布费率 v2（10 元/分）并核算新组合
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "10")], 2)
    rn = client.post(f"/shifts/{s1}/loss-accountings", json={"rate_version": 2})
    assert rn.status_code == 201

    rget = client.get("/loss-closings/LC-IMMUT")
    after = rget.json()
    assert after["rate_version"] == 1
    assert after["totals"] == before["totals"]
    assert after["items"][0]["accounting_id"] == before["items"][0]["accounting_id"]


def test_closing_errors(client):
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    client.post(f"/shifts/{s1}/settle")
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)

    base = {
        "device_id": "CNC-01",
        "closing_no": "LC-ERR",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }

    # 序号越界 → 422 INVALID_SEQ
    r = _close(client, {**base, "closing_no": "LC-ERR-SEQ", "seq": 999})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SEQ"

    # 未知费率版本 → 404 RATE_VERSION_NOT_FOUND
    r = _close(client, {**base, "closing_no": "LC-ERR-RV", "rate_version": 999})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"

    # 未知设备 → 404 DEVICE_NOT_FOUND（路径与正文一致才到服务层设备校验）
    r = _close(client, {**base, "device_id": "NOPE"}, device="NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"

    # 路径/正文设备不一致 → 422
    r = client.post(
        "/devices/NOPE/loss-closings", json={**base, "device_id": "CNC-01"}
    )
    assert r.status_code == 422

    # 非法期间（缺时区 / start≥end / 未对齐整分钟）→ 422 VALIDATION_ERROR
    r = _close(
        client,
        {**base, "closing_no": "LC-ERR-T1", "start": "2026-10-01T00:00:00"},
    )
    assert r.status_code == 422
    r = _close(
        client,
        {
            **base,
            "closing_no": "LC-ERR-T2",
            "start": "2026-10-02T00:00:00+08:00",
            "end": "2026-10-01T00:00:00+08:00",
        },
    )
    assert r.status_code == 422
    r = _close(
        client,
        {**base, "closing_no": "LC-ERR-T3", "seq": -1},
    )
    assert r.status_code == 422

    # 未知单号读取 → 404 CLOSING_NOT_FOUND
    assert client.get("/loss-closings/NOPE").status_code == 404
    assert client.get("/loss-closings/NOPE").json()["detail"]["code"] == (
        "CLOSING_NOT_FOUND"
    )

    # 越界检查在冲突检查之后不影响：先用越界 seq 的新单号被 422 拒绝（不应落库）
    # 失败的结账不留记录
    assert client.get("/loss-closings/LC-ERR-SEQ").status_code == 404


def test_segments_unavailable_fails_whole_closing(client):
    """任一选中修订无固化明细 → 422 SEGMENTS_UNAVAILABLE，整单不留痕。

    通过把班次 1 的修订 1 标记为 segments_available=FALSE 模拟结构引入前的
    历史修订（当时裁定状态无法可靠还原）。
    """
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-1", 1, "10:00:00", "11:00:00", "failure", day="01")
    client.post(f"/shifts/{s1}/settle")
    s2 = _shift(client, "02")
    _event(client, "PLC-2", 1, "09:00:00", "10:00:00", "failure", day="02")
    client.post(f"/shifts/{s2}/settle")
    _publish_rates(
        client, [_rate_entry("failure", "00:00:00", "23:59:00", "1", "01", "03")], 1
    )

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE shift_results SET segments_available = FALSE "
                "WHERE shift_id = :s AND revision = 1"
            ),
            {"s": s1},
        )

    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-BADSEG",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-03T00:00:00+08:00",
        "seq": 2,
        "rate_version": 1,
    }
    r = _close(client, body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"

    # 不留结账记录
    assert client.get("/loss-closings/LC-BADSEG").status_code == 404
    # 不留部分新账：两班次都不应产生任何损失账
    with engine.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) FROM loss_accountings WHERE device_id = 'CNC-01'")
        ).scalar()
        assert n == 0
        n_close = conn.execute(
            text("SELECT COUNT(*) FROM loss_period_closings")
        ).scalar()
        assert n_close == 0


def test_list_device_closings(client):
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    client.post(f"/shifts/{s1}/settle")
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)
    mk = {
        "device_id": "CNC-01",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }
    _close(client, {**mk, "closing_no": "LC-L1"})
    _close(client, {**mk, "closing_no": "LC-L2", "end": "2026-10-01T12:00:00+08:00"})

    r = client.get("/devices/CNC-01/loss-closings")
    assert r.status_code == 200
    nos = [c["closing_no"] for c in r.json()]
    assert nos == ["LC-L1", "LC-L2"]  # 按 id 升序

    assert client.get("/devices/NOPE/loss-closings").status_code == 404


def test_concurrent_same_no_same_input_single_closing(client):
    """同单号同输入并发提交：只固化一张结账，其余幂等返回，不留重复账。"""
    import threading

    client.post("/devices", json=DEVICE)
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    client.post(f"/shifts/{s1}/settle")
    _publish_rates(client, [_rate_entry("failure", "00:00:00", "23:59:00", "1")], 1)
    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-CONC",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }
    barrier = threading.Barrier(4)
    statuses: list[int] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=10)
            r = client.post("/devices/CNC-01/loss-closings", json=body)
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
    assert not errors, errors
    assert statuses.count(201) == 1
    assert statuses.count(200) == 3
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM loss_period_closings WHERE closing_no='LC-CONC'")
        ).scalar() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM loss_period_closing_items")
        ).scalar() == 1
        # 同 (班次,修订,费率) 的损失账只固化一份
        assert conn.execute(
            text("SELECT COUNT(*) FROM loss_accountings WHERE device_id='CNC-01'")
        ).scalar() == 1


def test_closing_isolates_device(client):
    """其他设备的费率版本号不能用于本设备结账 → 404。"""
    client.post("/devices", json=DEVICE)
    client.post("/devices", json={"id": "CNC-02", "name": "加工中心2号"})
    s1 = _shift(client, "01")
    _event(client, "PLC-9", 1, "10:00:00", "11:00:00", "failure")
    client.post(f"/shifts/{s1}/settle")
    # 费率版本只发给 CNC-02
    client.post(
        "/devices/CNC-02/rate-versions",
        json={"entries": [_rate_entry("failure", "00:00:00", "23:59:00", "1")]},
    )
    body = {
        "device_id": "CNC-01",
        "closing_no": "LC-XDEV",
        "start": "2026-10-01T00:00:00+08:00",
        "end": "2026-10-02T00:00:00+08:00",
        "seq": 1,
        "rate_version": 1,
    }
    r = _close(client, body)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "RATE_VERSION_NOT_FOUND"
