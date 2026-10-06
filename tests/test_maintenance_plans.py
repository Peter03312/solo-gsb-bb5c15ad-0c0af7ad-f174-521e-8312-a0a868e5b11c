"""计划保养提交与核对（POST /devices/{id}/maintenance-plans、
GET /maintenance-plans/{no}/check）端到端测试。

覆盖：
- 提交校验（窗口非空/落在期间内/互不重叠、同设备期间不重叠/端点相接允许）；
- 同单号同内容幂等（窗口换序、等价时区）、异内容 409；
- 核对四类分钟（已执行/未执行/计划外/未核实）、部分覆盖拆分、最大连续区间；
- seq 缺省固定最新序号、显式序号重放、序号越界、历史修订无固化段拒绝；
- 未结算班次不参与、裁定段计入保养、读取与列表、未知设备/单号错误。

运行方式：pytest tests/test_maintenance_plans.py
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "maintenance_plans",
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


def _shift(client, day=1, sh=8, eh=16, device="CNC-01"):
    r = client.post(
        "/shifts",
        json={
            "device_id": device,
            "start": f"2026-10-{day:02d}T{sh:02d}:00:00+08:00",
            "end": f"2026-10-{day:02d}T{eh:02d}:00:00+08:00",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _event(client, source, version, day, sh, eh, reason):
    def _fmt(x):
        if isinstance(x, int):
            return f"{x:02d}:00:00"
        hh, mm = x
        return f"{hh:02d}:{mm:02d}:00"

    r = client.post(
        "/events",
        json={
            "device_id": "CNC-01",
            "source_id": source,
            "version": version,
            "start": f"2026-10-{day:02d}T{_fmt(sh)}+08:00",
            "end": f"2026-10-{day:02d}T{_fmt(eh)}+08:00",
            "reason": reason,
        },
    )
    assert r.status_code in (200, 201), r.text


def _iv(day, sh, eh):
    return {"start": f"2026-10-{day:02d}T{sh}+08:00", "end": f"2026-10-{day:02d}T{eh}+08:00"}


def _plan_body(windows, plan_no="MP-001", start="2026-10-01T00:00:00+08:00",
               end="2026-10-02T00:00:00+08:00", device="CNC-01"):
    return {
        "device_id": device,
        "plan_no": plan_no,
        "start": start,
        "end": end,
        "windows": windows,
    }


def _submit(client, body, device="CNC-01"):
    return client.post(f"/devices/{device}/maintenance-plans", json=body)


def _check(client, plan_no="MP-001", seq=None):
    url = f"/maintenance-plans/{plan_no}/check"
    if seq is not None:
        url += f"?seq={seq}"
    return client.get(url)


# ---------------------------------------------------------------- 提交与幂等


def test_submit_plan_and_persist(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body([_iv(1, "10:00:00", "12:00:00")])
    r = _submit(client, body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["idempotent"] is False
    assert data["plan_no"] == "MP-001"
    assert data["device_id"] == "CNC-01"
    assert data["windows"] == [
        {
            "start": "2026-10-01T02:00:00Z",
            "end": "2026-10-01T04:00:00Z",
            "minutes": 120,
        }
    ]


def test_same_no_same_content_idempotent_window_reorder_and_timezone(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body(
        [
            _iv(1, "13:00:00", "14:00:00"),
            _iv(1, "10:00:00", "11:00:00"),
        ]
    )
    r1 = _submit(client, body)
    assert r1.status_code == 201
    # 重试：窗口换序；期间与窗口改用等价 UTC 表示（+00:00 / Z 等价 +08:00）
    body2 = _plan_body(
        [
            {"start": "2026-10-01T02:00:00+00:00", "end": "2026-10-01T03:00:00+00:00"},
            {"start": "2026-10-01T05:00:00+00:00", "end": "2026-10-01T06:00:00+00:00"},
        ],
        start="2026-09-30T16:00:00Z",
        end="2026-10-01T16:00:00Z",
    )
    r2 = _submit(client, body2)
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotent"] is True
    # 返回内容规范化一致（按起点升序、UTC）
    assert r2.json()["windows"] == r1.json()["windows"]
    assert r2.json()["id"] == r1.json()["id"]


def test_same_no_different_content_conflict(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body([_iv(1, "10:00:00", "12:00:00")])
    assert _submit(client, body).status_code == 201
    bad = _plan_body([_iv(1, "10:00:00", "11:00:00")])
    r = _submit(client, bad)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "MAINTENANCE_PLAN_CONFLICT"


def test_same_no_different_period_conflict(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body([_iv(1, "10:00:00", "12:00:00")])
    assert _submit(client, body).status_code == 201
    bad = _plan_body(
        [{"start": "2026-10-02T02:00:00+08:00", "end": "2026-10-02T04:00:00+08:00"}],
        start="2026-10-02T00:00:00+08:00",
        end="2026-10-03T00:00:00+08:00",
    )
    r = _submit(client, bad)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "MAINTENANCE_PLAN_CONFLICT"


def test_overlapping_period_same_device_rejected(client):
    client.post("/devices", json=DEVICE)
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "11:00:00")], "MP-A")).status_code == 201
    # 期间重叠
    r = _submit(
        client,
        _plan_body(
            [{"start": "2026-10-01T23:00:00+08:00", "end": "2026-10-01T23:30:00+08:00"}],
            plan_no="MP-B",
        ),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "MAINTENANCE_PLAN_OVERLAP"


def test_adjacent_periods_allowed(client):
    client.post("/devices", json=DEVICE)
    body1 = _plan_body(
        [_iv(1, "10:00:00", "11:00:00")],
        "MP-A",
        start="2026-10-01T00:00:00+08:00",
        end="2026-10-01T12:00:00+08:00",
    )
    body2 = _plan_body(
        [{"start": "2026-10-01T12:00:00+08:00", "end": "2026-10-01T13:00:00+08:00"}],
        "MP-B",
        start="2026-10-01T12:00:00+08:00",
        end="2026-10-02T00:00:00+08:00",
    )
    assert _submit(client, body1).status_code == 201
    assert _submit(client, body2).status_code == 201


def test_overlapping_windows_rejected(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body(
        [
            _iv(1, "10:00:00", "11:30:00"),
            _iv(1, "11:00:00", "12:00:00"),
        ]
    )
    r = _submit(client, body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MAINTENANCE_WINDOW_INVALID"


def test_adjacent_windows_allowed(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body(
        [
            _iv(1, "10:00:00", "11:00:00"),
            _iv(1, "11:00:00", "12:00:00"),
        ]
    )
    assert _submit(client, body).status_code == 201


def test_window_out_of_period_rejected(client):
    client.post("/devices", json=DEVICE)
    body = _plan_body(
        [{"start": "2026-09-30T23:00:00+08:00", "end": "2026-10-01T01:00:00+08:00"}]
    )
    r = _submit(client, body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MAINTENANCE_WINDOW_INVALID"


def test_submit_unknown_device_and_validation_errors(client):
    r = _submit(
        client, _plan_body([_iv(1, "10:00:00", "11:00:00")], device="NOPE"),
        device="NOPE",
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"

    client.post("/devices", json=DEVICE)
    # 空窗口列表 → 422（schema min_length）
    assert _submit(client, _plan_body([])).status_code == 422
    # 期间缺时区
    bad = _plan_body([_iv(1, "10:00:00", "11:00:00")])
    bad["start"] = "2026-10-01T00:00:00"
    assert _submit(client, bad).status_code == 422
    # body/device 不一致 → 422 VALIDATION_ERROR
    r = client.post(
        "/devices/NOPE/maintenance-plans",
        json=_plan_body([_iv(1, "10:00:00", "11:00:00")]),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------- 核对分类


def test_check_all_executed(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 12, "maintenance")
    client.post(f"/shifts/{sid}/settle")
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "12:00:00")])).status_code == 201

    r = _check(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seq"] == 1
    assert data["window_minutes"] == 120
    assert data["executed"] == [
        {"start": "2026-10-01T02:00:00Z", "end": "2026-10-01T04:00:00Z", "minutes": 120}
    ]
    assert data["executed_minutes"] == 120
    assert data["not_executed"] == []
    assert data["unplanned"] == []
    assert data["unverified"] == []
    assert data["covered_minutes"] == 480
    assert data["basis"] == [
        {
            "shift_id": sid,
            "revision": 1,
            "seq": 1,
            "shift_start": "2026-10-01T00:00:00Z",
            "shift_end": "2026-10-01T08:00:00Z",
        }
    ]


def test_check_window_with_normal_and_failure_minutes(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 11, "maintenance")
    _event(client, "PLC-9", 1, 1, 11, 12, "failure")
    client.post(f"/shifts/{sid}/settle")
    # 窗口 10:00-13:00：保养 60 已执行；故障 60 未执行；正常生产 12-13 共 60 未执行
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "13:00:00")])).status_code == 201
    data = _check(client).json()
    assert data["executed"] == [
        {"start": "2026-10-01T02:00:00Z", "end": "2026-10-01T03:00:00Z", "minutes": 60}
    ]
    assert data["not_executed"] == [
        {"start": "2026-10-01T03:00:00Z", "end": "2026-10-01T05:00:00Z", "minutes": 120}
    ]
    assert data["not_executed_minutes"] == 120
    assert data["unverified"] == []
    assert data["executed_minutes"] + data["not_executed_minutes"] + data[
        "unverified_minutes"
    ] == data["window_minutes"]


def test_check_unverified_outside_settled_shifts(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)  # 08-16
    _event(client, "MES-1", 1, 1, 10, 11, "maintenance")
    client.post(f"/shifts/{sid}/settle")
    # 窗口 10:00-18:00：10-11 已执行，11-16 在班次内无保养 → 未执行，16-18 无班次 → 未核实
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "18:00:00")])).status_code == 201
    data = _check(client).json()
    assert data["executed_minutes"] == 60
    assert data["not_executed"] == [
        {"start": "2026-10-01T03:00:00Z", "end": "2026-10-01T08:00:00Z", "minutes": 300}
    ]
    assert data["unverified"] == [
        {"start": "2026-10-01T08:00:00Z", "end": "2026-10-01T10:00:00Z", "minutes": 120}
    ]
    assert data["unverified_minutes"] == 120


def test_check_unsettled_shift_not_covered(client):
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 11, "maintenance")
    client.post(f"/shifts/{s1}/settle")
    _shift(client, 2)  # 10/2 08-16 未结算
    assert _submit(
        client,
        _plan_body(
            [_iv(1, "10:00:00", "11:00:00"), _iv(2, "10:00:00", "11:00:00")],
            start="2026-10-01T00:00:00+08:00",
            end="2026-10-03T00:00:00+08:00",
        )
    ).status_code == 201
    data = _check(client).json()
    assert [b["shift_id"] for b in data["basis"]] == [s1]
    assert data["executed_minutes"] == 60
    # 10/2 窗口 60 分钟未被已结算班次覆盖 → 未核实
    assert data["unverified"] == [
        {"start": "2026-10-02T02:00:00Z", "end": "2026-10-02T03:00:00Z", "minutes": 60}
    ]


def test_check_unplanned_maintenance_outside_windows(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 11, "maintenance")
    _event(client, "MES-2", 1, 1, 14, 15, "maintenance")
    client.post(f"/shifts/{sid}/settle")
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "11:00:00")])).status_code == 201
    data = _check(client).json()
    assert data["executed_minutes"] == 60
    assert data["unplanned"] == [
        {"start": "2026-10-01T06:00:00Z", "end": "2026-10-01T07:00:00Z", "minutes": 60}
    ]
    assert data["unplanned_minutes"] == 60


def test_check_pinned_seq_replay(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "MES-1", 1, 1, 10, 12, "maintenance")
    client.post(f"/shifts/{sid}/settle")  # seq=1：保养 10-12
    # 迟到更正：保养缩短为 10-11 → seq=2
    _event(client, "MES-1", 2, 1, 10, 11, "maintenance")
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "12:00:00")])).status_code == 201

    latest = _check(client).json()
    assert latest["seq"] == 2
    assert latest["executed_minutes"] == 60
    assert latest["not_executed_minutes"] == 60  # 11-12 班次内无保养
    assert [b["revision"] for b in latest["basis"]] == [2]

    pinned = _check(client, seq=1).json()
    assert pinned["seq"] == 1
    assert pinned["executed_minutes"] == 120
    assert pinned["not_executed_minutes"] == 0
    assert [b["revision"] for b in pinned["basis"]] == [1]

    # 再来一次更正后，seq=1 重放不变
    _event(client, "MES-1", 3, 1, 10, (10, 30), "maintenance")
    replayed = _check(client, seq=1).json()
    assert replayed["executed"] == pinned["executed"]
    assert replayed["basis"] == pinned["basis"]


def test_check_ruling_maintenance_counts_as_executed(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "PLC-9", 1, 1, 10, 11, "failure")
    client.post(f"/shifts/{sid}/settle")  # seq=1：10-11 故障
    # 裁定 14:00-15:00 为保养 → seq=2
    r = client.post(
        f"/shifts/{sid}/adjudications",
        json={
            "ruling_no": "R-1",
            **_iv(1, "14:00:00", "15:00:00"),
            "reason": "maintenance",
            "note": "计划保养已做",
        },
    )
    assert r.status_code == 201, r.text
    assert _submit(client, _plan_body([_iv(1, "14:00:00", "15:00:00")])).status_code == 201
    data = _check(client).json()
    assert data["seq"] == 2
    assert data["executed_minutes"] == 60
    assert data["not_executed_minutes"] == 0
    assert [b["revision"] for b in data["basis"]] == [2]


def test_check_window_split_across_two_shifts(client):
    client.post("/devices", json=DEVICE)
    s1 = _shift(client, 1, 8, 12)   # 08-12
    s2 = _shift(client, 1, 12, 16)  # 12-16（端点相接，设备内不重叠）
    _event(client, "A", 1, 1, 10, 12, "maintenance")
    client.post(f"/shifts/{s1}/settle")
    client.post(f"/shifts/{s2}/settle")
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "13:00:00")])).status_code == 201
    data = _check(client).json()
    # 10-12 保养已执行（跨段合并），12-13 班次内无保养未执行
    assert data["executed"] == [
        {"start": "2026-10-01T02:00:00Z", "end": "2026-10-01T04:00:00Z", "minutes": 120}
    ]
    assert data["not_executed"] == [
        {"start": "2026-10-01T04:00:00Z", "end": "2026-10-01T05:00:00Z", "minutes": 60}
    ]
    assert data["unverified"] == []
    assert [b["shift_id"] for b in data["basis"]] == [s1, s2]


def test_check_multiple_windows_maximal_intervals(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "A", 1, 1, 9, 12, "maintenance")
    client.post(f"/shifts/{sid}/settle")
    # 两个相接窗口 10-11、11-12；保养段连续覆盖 → 已执行合并为一个最大连续区间
    assert _submit(
        client,
        _plan_body([_iv(1, "11:00:00", "12:00:00"), _iv(1, "10:00:00", "11:00:00")]),
    ).status_code == 201
    data = _check(client).json()
    assert data["executed"] == [
        {"start": "2026-10-01T02:00:00Z", "end": "2026-10-01T04:00:00Z", "minutes": 120}
    ]
    # 09-10 保养在窗口外 → 计划外
    assert data["unplanned"] == [
        {"start": "2026-10-01T01:00:00Z", "end": "2026-10-01T02:00:00Z", "minutes": 60}
    ]


def test_check_empty_when_no_settled_shifts(client):
    client.post("/devices", json=DEVICE)
    _shift(client, 1)
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "12:00:00")])).status_code == 201
    data = _check(client).json()
    assert data["seq"] == 0
    assert data["basis"] == []
    assert data["executed"] == []
    assert data["not_executed"] == []
    assert data["unverified_minutes"] == 120


# ---------------------------------------------------------------- 读取与错误


def test_get_plan_and_listing(client):
    client.post("/devices", json=DEVICE)
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "11:00:00")])).status_code == 201
    r = client.get("/maintenance-plans/MP-001")
    assert r.status_code == 200
    assert r.json()["plan_no"] == "MP-001"

    r = client.get("/devices/CNC-01/maintenance-plans")
    assert r.status_code == 200
    assert [p["plan_no"] for p in r.json()] == ["MP-001"]


def test_unknown_plan_and_device_errors(client):
    r = _check(client, "NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MAINTENANCE_PLAN_NOT_FOUND"

    r = client.get("/devices/NOPE/maintenance-plans")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


def test_seq_beyond_latest_rejected(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "A", 1, 1, 10, 11, "maintenance")
    client.post(f"/shifts/{sid}/settle")  # seq=1
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "11:00:00")])).status_code == 201
    r = _check(client, seq=99)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_SEQ"


def test_legacy_revision_without_segments_rejected(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "A", 1, 1, 10, 11, "maintenance")
    client.post(f"/shifts/{sid}/settle")
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "11:00:00")])).status_code == 201
    with engine.begin() as conn:
        conn.execute(text("UPDATE shift_results SET segments_available = FALSE"))
    r = _check(client)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SEGMENTS_UNAVAILABLE"


def test_seq_zero_all_unverified(client):
    client.post("/devices", json=DEVICE)
    sid = _shift(client, 1)
    _event(client, "A", 1, 1, 10, 11, "maintenance")
    client.post(f"/shifts/{sid}/settle")  # seq=1
    assert _submit(client, _plan_body([_iv(1, "10:00:00", "11:00:00")])).status_code == 201
    r = _check(client, seq=0)
    assert r.status_code == 200
    data = r.json()
    assert data["seq"] == 0
    assert data["basis"] == []
    assert data["executed"] == []
    assert data["unverified_minutes"] == 60
