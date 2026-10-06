"""业务逻辑：设备、班次、事件版本链、结算与迟到更正重算。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import attribution, pricing, reconciliation, allocation, closing, maintenance
from . import fingerprints as fp
from .attribution import EventView, RulingView
from .config import settings
from .models import (
    ChangeCounter,
    Device,
    EventSource,
    EventVersion,
    LossAccounting,
    LossAccountingPiece,
    LossCostAllocation,
    LossCostAllocationAccount,
    LossCostAllocationSplit,
    LossPeriodClosing,
    LossPeriodClosingItem,
    MaintenancePlan,
    ManualAdjudication,
    RateEntry,
    RateVersion,
    Shift,
    ShiftResult,
    ShiftResultSegment,
)
from .schemas import (
    AdjudicationIn,
    DeviceCreate,
    EventBatchIn,
    EventIn,
    ShiftCreate,
)


class ApiError(HTTPException):
    """统一错误格式：{"detail": {"code": ..., "message": ...}}"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(
            status_code=status_code, detail={"code": code, "message": message}
        )


def _utc(dt: datetime) -> datetime:
    return fp.utc(dt)


def get_device_or_404(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"unknown device: {device_id!r}")
    return device


# ---------------------------------------------------------------- 设备


def create_device(db: Session, payload: DeviceCreate) -> tuple[Device, bool]:
    existing = db.get(Device, payload.id)
    if existing is not None:
        return existing, False
    device = Device(id=payload.id, name=payload.name)
    db.add(device)
    try:
        db.commit()
    except IntegrityError:  # 并发重复创建
        db.rollback()
        return db.get(Device, payload.id), False
    return device, True


def list_devices(db: Session) -> list[Device]:
    return list(db.execute(select(Device).order_by(Device.id)).scalars().all())


# ---------------------------------------------------------------- 班次


def create_shift(db: Session, payload: ShiftCreate) -> Shift:
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    conflict = (
        db.execute(
            select(Shift).where(
                Shift.device_id == payload.device_id,
                Shift.start_ts < end,
                Shift.end_ts > start,
            )
        )
        .scalars()
        .first()
    )
    if conflict is not None:
        raise ApiError(
            409,
            "SHIFT_OVERLAP",
            f"shift overlaps existing shift id={conflict.id} "
            f"[{conflict.start_ts.isoformat()}, {conflict.end_ts.isoformat()}) "
            f"on device {payload.device_id!r}",
        )
    shift = Shift(device_id=payload.device_id, start_ts=start, end_ts=end)
    db.add(shift)
    try:
        db.commit()
    except IntegrityError:  # 排他约束兜底（并发创建）
        db.rollback()
        raise ApiError(
            409, "SHIFT_OVERLAP", "shift overlaps an existing shift on this device"
        )
    db.refresh(shift)
    return shift


def get_shift_or_404(db: Session, shift_id: int) -> Shift:
    shift = db.get(Shift, shift_id)
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    return shift


# ---------------------------------------------------------------- 归因计算


def _allocate_seq(db: Session) -> int:
    """分配全局递增的变更序号。

    单例计数器行在写事务内 UPDATE ... RETURNING 递增，行锁持有至提交：
    序号顺序 == 提交顺序，同步游标因此不会越过尚未可见的较早变更；
    序号与修订在同一事务写入，同时提交或同时回滚。
    """
    db.execute(
        pg_insert(ChangeCounter).values(id=1, last_seq=0).on_conflict_do_nothing()
    )
    return db.execute(
        update(ChangeCounter)
        .where(ChangeCounter.id == 1)
        .values(last_seq=ChangeCounter.last_seq + 1)
        .returning(ChangeCounter.last_seq)
    ).scalar_one()


def _current_event_views(
    db: Session, device_id: str, start: datetime, end: datetime
) -> list[EventView]:
    """设备当前生效事件（每个来源取最新版本）中与窗口 [start, end) 重叠的部分。"""
    stmt = (
        select(EventVersion)
        .join(
            EventSource,
            and_(
                EventVersion.device_id == EventSource.device_id,
                EventVersion.source_id == EventSource.source_id,
                EventVersion.version == EventSource.last_version,
            ),
        )
        .where(
            EventVersion.device_id == device_id,
            EventVersion.start_ts < end,
            EventVersion.end_ts > start,
        )
    )
    rows = db.execute(stmt).scalars().all()
    return [
        EventView(
            source_id=r.source_id,
            version=r.version,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
        )
        for r in rows
    ]


def _latest_revision(db: Session, shift_id: int) -> ShiftResult | None:
    return (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _compute(db: Session, shift: Shift) -> tuple[dict, list[dict], list[dict]]:
    """按当前生效事件与裁定计算班次的 (totals, used_versions, segments)。"""
    events = _current_event_views(db, shift.device_id, shift.start_ts, shift.end_ts)
    rulings = _active_ruling_views(db, shift.id)
    totals, used = attribution.attribute_with_rulings(
        shift.start_ts, shift.end_ts, events, rulings
    )
    segments = attribution.attribute_segments(
        shift.start_ts, shift.end_ts, events, rulings
    )
    return totals, used, segments


def _active_ruling_views(db: Session, shift_id: int) -> list[RulingView]:
    """班次当前生效（未撤销）的现场人工裁定；id 即生效顺序。"""
    rows = (
        db.execute(
            select(ManualAdjudication)
            .where(
                ManualAdjudication.shift_id == shift_id,
                ManualAdjudication.status == "active",
            )
            .order_by(ManualAdjudication.id)
        )
        .scalars()
        .all()
    )
    return [
        RulingView(
            ruling_no=r.ruling_no,
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
            seq=r.id,
        )
        for r in rows
    ]


def _new_revision(
    db: Session,
    shift: Shift,
    totals: dict,
    used: list[dict],
    trigger: str,
    base: ShiftResult | None,
    segments: list[dict],
) -> ShiftResult:
    if base is None:
        revision, bm, bf, bp = 1, 0, 0, 0
    else:
        revision = base.revision + 1
        bm, bf, bp = (
            base.maintenance_minutes,
            base.failure_minutes,
            base.pending_minutes,
        )
    rev = ShiftResult(
        shift_id=shift.id,
        revision=revision,
        seq=_allocate_seq(db),
        maintenance_minutes=totals["maintenance"],
        failure_minutes=totals["failure"],
        pending_minutes=totals["pending"],
        d_maintenance=totals["maintenance"] - bm,
        d_failure=totals["failure"] - bf,
        d_pending=totals["pending"] - bp,
        event_versions=used,
        trigger=trigger,
        segments_available=True,
    )
    db.add(rev)
    # 归因区间明细与修订同事务固化：只插入、之后不更新不删除
    for seg_no, seg in enumerate(segments, start=1):
        db.add(
            ShiftResultSegment(
                shift_id=shift.id,
                revision=revision,
                seg_no=seg_no,
                start_ts=seg["start"],
                end_ts=seg["end"],
                reason=seg["reason"],
                basis=seg["basis"],
            )
        )
    return rev


# ---------------------------------------------------------------- 结算


def settle_shift(db: Session, shift_id: int) -> tuple[Shift, ShiftResult, bool]:
    """结算班次：计算并写入修订 1；已结算则幂等返回最新修订。"""
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    latest = _latest_revision(db, shift_id)
    if latest is not None:
        return shift, latest, False
    totals, used, segments = _compute(db, shift)
    rev = _new_revision(
        db, shift, totals, used, trigger="settle", base=None, segments=segments
    )
    db.commit()
    db.refresh(rev)
    return shift, rev, True


# ---------------------------------------------------------------- 事件上报


def ingest_event(
    db: Session, payload: EventIn
) -> tuple[EventVersion, bool, list[tuple[Shift, ShiftResult]]]:
    """上报事件版本。

    返回 (事件版本, 是否幂等命中, 因更正而重算的 [(班次, 新修订)])。
    """
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    digest = fp.event_fingerprint(start, end, payload.reason)

    # 确保版本链游标存在并加行锁，串行化同一来源的并发上报
    db.execute(
        pg_insert(EventSource)
        .values(
            device_id=payload.device_id, source_id=payload.source_id, last_version=0
        )
        .on_conflict_do_nothing()
    )
    src = db.execute(
        select(EventSource)
        .where(
            EventSource.device_id == payload.device_id,
            EventSource.source_id == payload.source_id,
        )
        .with_for_update()
    ).scalar_one()

    if payload.version <= src.last_version:
        existing = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == payload.version,
            )
        ).scalar_one()
        if existing.content_hash == digest:
            db.commit()
            return existing, True, []  # 同来源同版本同内容：幂等返回
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            f"version {payload.version} of source {payload.source_id!r} "
            "already exists with different content",
        )
    if payload.version > src.last_version + 1:
        raise ApiError(
            409,
            "VERSION_GAP",
            f"source {payload.source_id!r} expects version "
            f"{src.last_version + 1}, got {payload.version}",
        )

    prev = None
    if src.last_version > 0:
        prev = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == src.last_version,
            )
        ).scalar_one()

    ev = EventVersion(
        device_id=payload.device_id,
        source_id=payload.source_id,
        version=payload.version,
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        content_hash=digest,
    )
    db.add(ev)
    src.last_version = payload.version
    src.last_content_hash = digest
    db.flush()

    intervals = [(start, end)]
    if prev is not None:
        intervals.append((prev.start_ts, prev.end_ts))
    recomputed = _recompute_affected_shifts(db, payload.device_id, intervals)
    db.commit()
    return ev, False, recomputed


def ingest_event_batch(
    db: Session, payload: EventBatchIn
) -> tuple[list[dict], list[tuple[Shift, ShiftResult]]]:
    """断网补传：同一设备的非空事件列表，整批核对、整批应用（全或无）。

    返回 (逐项回执 [{"source_id", "version", "idempotent"}], 重算的 [(班次, 新修订)])。

    - 按列表顺序核对每个来源的版本链：已存在的同版本同内容项为幂等重试；
      同版本异内容 / 跳号 / 未知设备 / 任一项非法 → 整批失败，
      不留下事件、修订或变更（单事务，失败即整体回滚）；
    - 全部核对通过后一次性应用所有新版本，仅以批前和批后各来源最新版本的
      区间并集确定受影响的已结算班次，再按最终状态重算；
      每班次最多产生一次修订和一条变更，分钟数未变则都不产生。
    """
    get_device_or_404(db, payload.device_id)

    # 涉及的来源行：不存在则补建；按 source_id 固定顺序加行锁，
    # 与单条上报及其他并发批次相互串行化，维持连续版本链
    source_ids = sorted({item.source_id for item in payload.events})
    for sid in source_ids:
        db.execute(
            pg_insert(EventSource)
            .values(device_id=payload.device_id, source_id=sid, last_version=0)
            .on_conflict_do_nothing()
        )
    sources = (
        db.execute(
            select(EventSource)
            .where(
                EventSource.device_id == payload.device_id,
                EventSource.source_id.in_(source_ids),
            )
            .order_by(EventSource.source_id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    src_by_id = {s.source_id: s for s in sources}
    expected = {s.source_id: s.last_version for s in sources}

    # 按列表顺序逐项核对；全部通过前不写入任何数据
    pending: dict[tuple[str, int], str] = {}  # 本批已接受新版本的 (来源, 版本) -> 内容哈希
    new_items: list[tuple] = []  # (item, start_utc, end_utc, digest)
    acks: list[dict] = []
    for item in payload.events:
        start, end = _utc(item.start), _utc(item.end)
        digest = fp.event_fingerprint(start, end, item.reason)
        exp = expected[item.source_id]
        if item.version <= exp:
            # 已存在（库中或本批已接受）的同版本：同内容可重试，异内容整批失败
            existing = pending.get((item.source_id, item.version))
            if existing is None:
                existing = db.execute(
                    select(EventVersion.content_hash).where(
                        EventVersion.device_id == payload.device_id,
                        EventVersion.source_id == item.source_id,
                        EventVersion.version == item.version,
                    )
                ).scalar_one()
            if existing != digest:
                raise ApiError(
                    409,
                    "VERSION_CONFLICT",
                    f"version {item.version} of source {item.source_id!r} "
                    "already exists with different content; batch rejected",
                )
            acks.append(
                {
                    "source_id": item.source_id,
                    "version": item.version,
                    "idempotent": True,
                }
            )
        elif item.version == exp + 1:
            # 连续下一版本：接受为新版本，推进该来源的版本链
            expected[item.source_id] = item.version
            pending[(item.source_id, item.version)] = digest
            new_items.append((item, start, end, digest))
            acks.append(
                {
                    "source_id": item.source_id,
                    "version": item.version,
                    "idempotent": False,
                }
            )
        else:
            raise ApiError(
                409,
                "VERSION_GAP",
                f"source {item.source_id!r} expects version {exp + 1}, "
                f"got {item.version}; batch rejected",
            )

    recomputed: list[tuple[Shift, ShiftResult]] = []
    if new_items:
        # 应用全部新版本；同一来源取批内最后一个新版本推进版本链游标
        last_new: dict[str, tuple] = {}
        for item, start, end, digest in new_items:
            db.add(
                EventVersion(
                    device_id=payload.device_id,
                    source_id=item.source_id,
                    version=item.version,
                    start_ts=start,
                    end_ts=end,
                    reason=item.reason,
                    content_hash=digest,
                )
            )
            last_new[item.source_id] = (item, start, end, digest)

        # 受影响区间 = 各来源（批前最新版本区间 ∪ 批后最新版本区间）的并集
        intervals: list[tuple[datetime, datetime]] = []
        for sid, (item, start, end, digest) in last_new.items():
            src = src_by_id[sid]
            if src.last_version > 0:
                prev_start, prev_end = db.execute(
                    select(EventVersion.start_ts, EventVersion.end_ts).where(
                        EventVersion.device_id == payload.device_id,
                        EventVersion.source_id == sid,
                        EventVersion.version == src.last_version,
                    )
                ).one()
                intervals.append((prev_start, prev_end))
            intervals.append((start, end))
            src.last_version = item.version
            src.last_content_hash = digest
        db.flush()

        recomputed = _recompute_affected_shifts(db, payload.device_id, intervals)

    db.commit()
    return acks, recomputed


def _shifts_overlapping(
    db: Session,
    device_id: str,
    intervals: list[tuple[datetime, datetime]],
    *,
    for_update: bool = False,
) -> list[Shift]:
    """与任一区间相交（半开区间判定）的班次，按 id 升序（固定加锁顺序）。"""
    stmt = (
        select(Shift)
        .where(
            Shift.device_id == device_id,
            or_(
                *[
                    and_(Shift.start_ts < end, Shift.end_ts > start)
                    for start, end in intervals
                ]
            ),
        )
        .order_by(Shift.id)  # 固定加锁顺序，降低并发更正相互死锁的概率
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(db.execute(stmt).scalars().all())


def _recompute_affected_shifts(
    db: Session,
    device_id: str,
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[Shift, ShiftResult]]:
    """迟到更正：按事件时间（相关来源批前/批后版本区间的并集）找出受影响的
    已结算班次，用全部来源的最新版本与当前生效裁定重算；结果有变化时追加
    新修订并记录差额。每个班次在此至多重算一次（至多一个新修订）。
    """
    if not intervals:
        return []
    shifts = _shifts_overlapping(db, device_id, intervals, for_update=True)

    recomputed: list[tuple[Shift, ShiftResult]] = []
    for shift in shifts:
        latest = _latest_revision(db, shift.id)
        if latest is None:
            continue  # 尚未结算：结算时自然会使用最新事件版本
        totals, used, segments = _compute(db, shift)
        if (
            totals["maintenance"] == latest.maintenance_minutes
            and totals["failure"] == latest.failure_minutes
            and totals["pending"] == latest.pending_minutes
        ):
            continue  # 结果未变化，不产生空修订
        rev = _new_revision(
            db, shift, totals, used, trigger="correction", base=latest,
            segments=segments,
        )
        db.flush()
        recomputed.append((shift, rev))
    return recomputed


# ---------------------------------------------------------------- 事件预演与确认

#: 确认凭据类型标识（与快照续页游标区分，混用一律视为非法凭据）
_PREVIEW_TOKEN_TYPE = "event_preview"


def _compute_with_candidate(
    db: Session,
    shift: Shift,
    source_id: str,
    version: int,
    start: datetime,
    end: datetime,
    reason: str,
) -> tuple[dict, list[dict]]:
    """以候选版本替换其来源的当前最新版本，按全部来源最新版本与生效裁定计算。"""
    events = [
        ev
        for ev in _current_event_views(
            db, shift.device_id, shift.start_ts, shift.end_ts
        )
        if ev.source_id != source_id
    ]
    events.append(
        EventView(
            source_id=source_id, version=version, reason=reason, start=start, end=end
        )
    )
    rulings = _active_ruling_views(db, shift.id)
    return attribution.attribute_with_rulings(
        shift.start_ts, shift.end_ts, events, rulings
    )


def _shift_state_fingerprint(db: Session, shift: Shift, latest: ShiftResult) -> str:
    """影响班次结果的输入状态指纹。

    覆盖：班次当前最新修订号、与班次窗口相交的各来源最新事件版本（事件版本行
    落库后不可变，(来源, 版本) 即内容标识）、全部现场裁定（含已撤销；裁定行
    不可变，仅状态可变）的状态。任一变化都会改变指纹，确认时据此判定状态已变。
    """
    events = _current_event_views(db, shift.device_id, shift.start_ts, shift.end_ts)
    rulings = db.execute(
        select(ManualAdjudication.id, ManualAdjudication.status)
        .where(ManualAdjudication.shift_id == shift.id)
        .order_by(ManualAdjudication.id)
    ).all()
    body = {
        "rev": latest.revision,
        "events": sorted(f"{e.source_id}:{e.version}" for e in events),
        "rulings": [f"{rid}:{status}" for rid, status in rulings],
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def encode_preview_token(
    device_id: str,
    source_id: str,
    version: int,
    start: datetime,
    end: datetime,
    reason: str,
    src_last_version: int,
    shifts: list[dict],
) -> str:
    """把预演状态编码为不透明确认凭据（base64url(JSON) + HMAC-SHA256 签名，
    与快照续页游标同一密钥与格式）。

    shifts 为按班次 id 升序的 [{"id", "rev", "fp"}]（相关已结算班次及其结果
    输入状态指纹）；确认时逐项复核，任何状态已变即拒绝提交。
    """
    body = {
        "type": _PREVIEW_TOKEN_TYPE,
        "device_id": device_id,
        "source_id": source_id,
        "version": version,
        "start": start.astimezone(timezone.utc).isoformat(),
        "end": end.astimezone(timezone.utc).isoformat(),
        "reason": reason,
        "src_last_version": src_last_version,
        "shifts": shifts,
    }
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    mac = _snapshot_cursor_mac(payload)  # 与快照游标共用的 HMAC-SHA256 签名
    return (
        base64.urlsafe_b64encode(payload).rstrip(b"=")
        + b"."
        + base64.urlsafe_b64encode(mac).rstrip(b"=")
    ).decode()


def decode_preview_token(token: str) -> dict:
    """解码并验签确认凭据；伪造/篡改/损坏/类型不符一律视为非法凭据。"""
    invalid = ApiError(
        422,
        "INVALID_CONFIRM_TOKEN",
        "confirm token is invalid, malformed or tampered with; "
        "obtain a fresh one from POST /events/preview",
    )
    parts = token.split(".")
    if len(parts) != 2:
        raise invalid

    def _b64(seg: str) -> bytes:
        return seg.encode() + b"=" * (-len(seg) % 4)

    try:
        payload = base64.urlsafe_b64decode(_b64(parts[0]))
        mac = base64.urlsafe_b64decode(_b64(parts[1]))
        body = json.loads(payload)
    except (ValueError, json.JSONDecodeError):
        raise invalid
    if not isinstance(body, dict) or not hmac.compare_digest(
        mac, _snapshot_cursor_mac(payload)
    ):
        raise invalid
    try:
        if body.get("type") != _PREVIEW_TOKEN_TYPE:
            raise ValueError
        start = datetime.fromisoformat(body["start"])
        end = datetime.fromisoformat(body["end"])
        version = int(body["version"])
        src_last_version = int(body["src_last_version"])
        device_id = str(body["device_id"])
        source_id = str(body["source_id"])
        reason = str(body["reason"])
        shifts = [
            {"id": int(s["id"]), "rev": int(s["rev"]), "fp": str(s["fp"])}
            for s in body["shifts"]
        ]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise invalid
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or not start < end
        or version < 1
        or src_last_version < 0
        or reason not in attribution.REASONS
    ):
        raise invalid
    return {
        "device_id": device_id,
        "source_id": source_id,
        "version": version,
        "start": start,
        "end": end,
        "reason": reason,
        "src_last_version": src_last_version,
        "shifts": shifts,
    }


def preview_event(db: Session, payload: EventIn) -> tuple[bool, list[dict], str]:
    """只读预演：核对候选版本并计算其对已结算班次的影响，不写入任何数据。

    校验与版本链规则同单条上报（幂等 / 同版本异内容 / 跳号 / 未知设备）。
    返回 (是否幂等命中, 影响列表, 确认凭据)；影响按班次 id 升序，以旧版与
    候选版区间并集确定相关已结算班次，分钟数不变的班次也列出（changed=False）。
    """
    get_device_or_404(db, payload.device_id)
    start, end = _utc(payload.start), _utc(payload.end)
    digest = fp.event_fingerprint(start, end, payload.reason)

    # 只读：不补建来源行、不加锁；状态竞争由确认时的逐项复核兜底
    src = db.execute(
        select(EventSource).where(
            EventSource.device_id == payload.device_id,
            EventSource.source_id == payload.source_id,
        )
    ).scalar_one_or_none()
    last_version = src.last_version if src is not None else 0

    if payload.version <= last_version:
        existing = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == payload.version,
            )
        ).scalar_one()
        if existing.content_hash == digest:
            # 同来源同版本同内容：确认将按现有幂等语义返回，无任何影响
            token = encode_preview_token(
                payload.device_id,
                payload.source_id,
                payload.version,
                start,
                end,
                payload.reason,
                last_version,
                [],
            )
            return True, [], token
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            f"version {payload.version} of source {payload.source_id!r} "
            "already exists with different content",
        )
    if payload.version > last_version + 1:
        raise ApiError(
            409,
            "VERSION_GAP",
            f"source {payload.source_id!r} expects version "
            f"{last_version + 1}, got {payload.version}",
        )

    # 相关班次：旧版本区间 ∪ 候选版本区间 内的已结算班次
    intervals = [(start, end)]
    if last_version > 0:
        prev = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == payload.device_id,
                EventVersion.source_id == payload.source_id,
                EventVersion.version == last_version,
            )
        ).scalar_one()
        intervals.append((prev.start_ts, prev.end_ts))

    impacts: list[dict] = []
    token_shifts: list[dict] = []
    for shift in _shifts_overlapping(db, payload.device_id, intervals):
        latest = _latest_revision(db, shift.id)
        if latest is None:
            continue  # 未结算：结算时自然采用最新版本，不属于对已结算班次的影响
        proposed, _used = _compute_with_candidate(
            db, shift, payload.source_id, payload.version, start, end, payload.reason
        )
        current = {
            "maintenance": latest.maintenance_minutes,
            "failure": latest.failure_minutes,
            "pending": latest.pending_minutes,
        }
        delta = {k: proposed[k] - current[k] for k in current}
        impacts.append(
            {
                "shift_id": shift.id,
                "revision": latest.revision,
                "current": current,
                "proposed": proposed,
                "delta": delta,
                "changed": any(delta.values()),
            }
        )
        token_shifts.append(
            {
                "id": shift.id,
                "rev": latest.revision,
                "fp": _shift_state_fingerprint(db, shift, latest),
            }
        )

    token = encode_preview_token(
        payload.device_id,
        payload.source_id,
        payload.version,
        start,
        end,
        payload.reason,
        last_version,
        token_shifts,
    )
    return False, impacts, token


def _preview_stale() -> ApiError:
    return ApiError(
        409,
        "PREVIEW_STALE",
        "state has changed since the preview; nothing was written, "
        "re-run POST /events/preview and confirm with the fresh token",
    )


def confirm_event(
    db: Session, token: str
) -> tuple[EventVersion, bool, list[tuple[Shift, ShiftResult]]]:
    """凭预演确认凭据原子提交候选事件版本。

    - 重新核对候选内容、来源版本链及影响结果的班次/事件/裁定状态，
      任一已变 → 409 PREVIEW_STALE，不写入任何数据（不得部分写入）；
    - 状态未变 → 同事务落库事件并沿用现有更正规则重算（仅分钟数变化的
      班次产生修订与变更）；
    - 相同确认重试遵循现有事件幂等语义：同版本同内容幂等返回，
      同版本异内容 / 跳号沿用既有错误约定。
    """
    state = decode_preview_token(token)
    device_id = state["device_id"]
    get_device_or_404(db, device_id)
    start, end = state["start"], state["end"]
    digest = fp.event_fingerprint(start, end, state["reason"])

    # 与单条上报共用同一版本链行锁，串行化并发上报/确认
    db.execute(
        pg_insert(EventSource)
        .values(device_id=device_id, source_id=state["source_id"], last_version=0)
        .on_conflict_do_nothing()
    )
    src = db.execute(
        select(EventSource)
        .where(
            EventSource.device_id == device_id,
            EventSource.source_id == state["source_id"],
        )
        .with_for_update()
    ).scalar_one()

    version = state["version"]
    if version <= src.last_version:
        existing = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == device_id,
                EventVersion.source_id == state["source_id"],
                EventVersion.version == version,
            )
        ).scalar_one()
        if existing.content_hash == digest:
            db.commit()
            return existing, True, []  # 相同确认重试（或同内容已上报）：幂等
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            f"version {version} of source {state['source_id']!r} "
            "already exists with different content",
        )
    if version > src.last_version + 1:
        raise ApiError(
            409,
            "VERSION_GAP",
            f"source {state['source_id']!r} expects version "
            f"{src.last_version + 1}, got {version}",
        )
    if src.last_version != state["src_last_version"]:
        raise _preview_stale()  # 来源版本链在预演后已推进

    intervals = [(start, end)]
    if src.last_version > 0:
        prev = db.execute(
            select(EventVersion).where(
                EventVersion.device_id == device_id,
                EventVersion.source_id == state["source_id"],
                EventVersion.version == src.last_version,
            )
        ).scalar_one()
        intervals.append((prev.start_ts, prev.end_ts))

    # 复核相关已结算班次集合及各班次的结果输入状态（班次行锁内，与结算/
    # 更正/裁定写入相互串行；全部核对通过前不写入任何数据）
    shifts = _shifts_overlapping(db, device_id, intervals, for_update=True)
    settled: list[tuple[Shift, ShiftResult]] = []
    for shift in shifts:
        latest = _latest_revision(db, shift.id)
        if latest is not None:
            settled.append((shift, latest))
    expected = state["shifts"]
    if len(settled) != len(expected):
        raise _preview_stale()
    for (shift, latest), exp in zip(settled, expected):
        if shift.id != exp["id"] or latest.revision != exp["rev"]:
            raise _preview_stale()
        if _shift_state_fingerprint(db, shift, latest) != exp["fp"]:
            raise _preview_stale()

    # 状态未变：原子提交候选版本，沿用现有更正规则重算受影响班次
    ev = EventVersion(
        device_id=device_id,
        source_id=state["source_id"],
        version=version,
        start_ts=start,
        end_ts=end,
        reason=state["reason"],
        content_hash=digest,
    )
    db.add(ev)
    src.last_version = version
    src.last_content_hash = digest
    db.flush()
    recomputed = _recompute_affected_shifts(db, device_id, intervals)
    db.commit()
    return ev, False, recomputed


# ---------------------------------------------------------------- 现场人工裁定


def _get_locked_settled_shift(db: Session, shift_id: int) -> Shift:
    """取出班次行（FOR UPDATE 串行化该班次的所有写操作）并要求已结算。"""
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")
    if _latest_revision(db, shift_id) is None:
        raise ApiError(
            409,
            "SHIFT_NOT_SETTLED",
            f"shift id={shift_id} has not been settled; "
            "manual adjudication is only allowed after settlement",
        )
    return shift


def _recompute_after_ruling(
    db: Session, shift: Shift, trigger: str
) -> ShiftResult | None:
    """裁定/撤销后重算班次：仅当各原因分钟数变化时追加新修订并记录差额。"""
    latest = _latest_revision(db, shift.id)
    totals, used, segments = _compute(db, shift)
    if (
        totals["maintenance"] == latest.maintenance_minutes
        and totals["failure"] == latest.failure_minutes
        and totals["pending"] == latest.pending_minutes
    ):
        return None  # 分钟数无变化：无修订、无变更，历史修订保持不变
    rev = _new_revision(db, shift, totals, used, trigger=trigger, base=latest,
                        segments=segments)
    db.flush()
    return rev


def submit_adjudication(
    db: Session, shift_id: int, payload: AdjudicationIn
) -> tuple[Shift, ManualAdjudication, bool, ShiftResult | None, int]:
    """为已结算班次提交现场人工裁定。

    返回 (班次, 裁定, 是否幂等命中, 新修订或 None, 当前最新修订号)。
    - 同单号同内容重试：幂等返回，不产生修订；
    - 同单号异内容（含已撤销单号复用）：409 RULING_CONFLICT；
    - 区间越出班次 / 班次未结算 / 未知班次：分别明确报错。
    """
    shift = _get_locked_settled_shift(db, shift_id)
    start, end = _utc(payload.start), _utc(payload.end)
    if start < shift.start_ts or end > shift.end_ts:
        raise ApiError(
            422,
            "RULING_OUT_OF_RANGE",
            f"ruling [{start.isoformat()}, {end.isoformat()}) is not within "
            f"shift [{shift.start_ts.isoformat()}, {shift.end_ts.isoformat()})",
        )
    note = payload.note or ""
    digest = fp.ruling_fingerprint(start, end, payload.reason, note)

    existing = db.execute(
        select(ManualAdjudication).where(
            ManualAdjudication.shift_id == shift_id,
            ManualAdjudication.ruling_no == payload.ruling_no,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash == digest:
            db.commit()  # 同单号同内容（含已撤销后再提交同内容）：幂等
            return shift, existing, True, None, _latest_revision(db, shift_id).revision
        raise ApiError(
            409,
            "RULING_CONFLICT",
            f"ruling_no {payload.ruling_no!r} already exists on shift "
            f"{shift_id} with different content",
        )

    ruling = ManualAdjudication(
        shift_id=shift_id,
        ruling_no=payload.ruling_no,
        start_ts=start,
        end_ts=end,
        reason=payload.reason,
        note=note,
        content_hash=digest,
        status="active",
    )
    db.add(ruling)
    try:
        db.flush()  # 班次行锁内插入，id 顺序即裁定生效顺序；唯一约束兜底并发竞争
    except IntegrityError:
        db.rollback()
        # 并发同单号提交：重新判定幂等 / 冲突
        shift2 = _get_locked_settled_shift(db, shift_id)
        other = db.execute(
            select(ManualAdjudication).where(
                ManualAdjudication.shift_id == shift_id,
                ManualAdjudication.ruling_no == payload.ruling_no,
            )
        ).scalar_one()
        if other.content_hash != digest:
            raise ApiError(
                409,
                "RULING_CONFLICT",
                f"ruling_no {payload.ruling_no!r} already exists on shift "
                f"{shift_id} with different content",
            )
        return (
            shift2,
            other,
            True,
            None,
            _latest_revision(db, shift_id).revision,
        )

    rev = _recompute_after_ruling(db, shift, trigger="adjudication")
    db.commit()
    db.refresh(ruling)
    latest_rev = _latest_revision(db, shift_id)
    return shift, ruling, False, rev, latest_rev.revision


def revoke_adjudication(
    db: Session, shift_id: int, ruling_no: str
) -> tuple[Shift, ManualAdjudication, bool, ShiftResult | None, int]:
    """按单号撤销班次的现场人工裁定。

    返回 (班次, 裁定, 是否幂等命中, 新修订或 None, 当前最新修订号)。
    - 已撤销再撤销：幂等返回，不产生修订；
    - 未知班次 / 未知单号：分别 404；班次未结算不允许出现裁定，同样明确拒绝。
    撤销生效后重叠分钟回退到此前仍有效的裁定；无裁定时按当前最新事件版本计算。
    """
    shift = _get_locked_settled_shift(db, shift_id)
    ruling = db.execute(
        select(ManualAdjudication).where(
            ManualAdjudication.shift_id == shift_id,
            ManualAdjudication.ruling_no == ruling_no,
        )
    ).scalar_one_or_none()
    if ruling is None:
        raise ApiError(
            404,
            "RULING_NOT_FOUND",
            f"unknown ruling_no {ruling_no!r} on shift {shift_id}",
        )
    if ruling.status == "revoked":
        db.commit()  # 重复撤销幂等
        return shift, ruling, True, None, _latest_revision(db, shift_id).revision

    ruling.status = "revoked"
    ruling.revoked_at = datetime.now(timezone.utc)
    db.flush()
    rev = _recompute_after_ruling(db, shift, trigger="adjudication_revoke")
    db.commit()
    db.refresh(ruling)
    latest_rev = _latest_revision(db, shift_id)
    return shift, ruling, False, rev, latest_rev.revision


def list_adjudications(db: Session, shift_id: int) -> tuple[Shift, list]:
    """列出班次的全部现场人工裁定（含已撤销），按生效顺序排列。"""
    shift = get_shift_or_404(db, shift_id)
    rows = (
        db.execute(
            select(ManualAdjudication)
            .where(ManualAdjudication.shift_id == shift_id)
            .order_by(ManualAdjudication.id)
        )
        .scalars()
        .all()
    )
    return shift, list(rows)


# ---------------------------------------------------------------- 结果查询


def get_result(
    db: Session, shift_id: int, revision: int | None
) -> tuple[Shift, ShiftResult, int]:
    """返回 (班次, 指定/最新修订, 最新修订号)。"""
    shift = get_shift_or_404(db, shift_id)
    latest = _latest_revision(db, shift_id)
    if latest is None:
        raise ApiError(
            404, "RESULT_NOT_FOUND", f"shift id={shift_id} has not been settled"
        )
    if revision is None:
        return shift, latest, latest.revision
    rev = db.execute(
        select(ShiftResult).where(
            ShiftResult.shift_id == shift_id, ShiftResult.revision == revision
        )
    ).scalar_one_or_none()
    if rev is None:
        raise ApiError(
            404, "REVISION_NOT_FOUND", f"shift id={shift_id} has no revision {revision}"
        )
    return shift, rev, latest.revision


def list_revisions(db: Session, shift_id: int) -> tuple[Shift, list[ShiftResult]]:
    shift = get_shift_or_404(db, shift_id)
    revs = (
        db.execute(
            select(ShiftResult)
            .where(ShiftResult.shift_id == shift_id)
            .order_by(ShiftResult.revision)
        )
        .scalars()
        .all()
    )
    return shift, list(revs)


def get_result_segments(
    db: Session, shift_id: int, revision: int | None
) -> tuple[Shift, ShiftResult, int, list[ShiftResultSegment] | None]:
    """班次某修订的归因区间明细。

    返回 (班次, 修订, 最新修订号, 明细段列表或 None)；None 表示该修订未固化
    明细（本结构引入前生成的历史修订，当时的裁定状态无法可靠还原），
    调用方应明确返回明细不可用，不得以当前状态推算。
    错误语义与 get_result 一致：未知班次 404 SHIFT_NOT_FOUND、
    未结算 404 RESULT_NOT_FOUND、未知修订 404 REVISION_NOT_FOUND。
    """
    shift, rev, latest_revision = get_result(db, shift_id, revision)
    if not rev.segments_available:
        return shift, rev, latest_revision, None
    rows = (
        db.execute(
            select(ShiftResultSegment)
            .where(
                ShiftResultSegment.shift_id == shift_id,
                ShiftResultSegment.revision == rev.revision,
            )
            .order_by(ShiftResultSegment.seg_no)
        )
        .scalars()
        .all()
    )
    return shift, rev, latest_revision, list(rows)


# ---------------------------------------------------------------- 变更同步

#: GET /changes 允许的页大小上限
MAX_PAGE_SIZE = 500


def list_changes(
    db: Session, cursor: int, limit: int
) -> tuple[list[tuple[ShiftResult, Shift]], int]:
    """按全局递增游标分页读取结算/更正变更，返回 ([(修订, 班次)], 下一游标)。

    - 返回 seq > cursor 的变更，按 seq 升序，最多 limit 条；
    - 空页保持原游标（客户端已追平，可原地轮询）；
    - 游标超过当前最大序号视为非法（客户端进度与服务端不一致），
      明确拒绝且不影响任何同步进度。
    """
    if cursor > 0:
        max_seq = db.execute(select(func.max(ShiftResult.seq))).scalar() or 0
        if cursor > max_seq:
            raise ApiError(
                422,
                "INVALID_CURSOR",
                f"cursor {cursor} is beyond the latest change seq {max_seq}; "
                "restart sync from a smaller cursor (0 replays from the beginning)",
            )
    rows = db.execute(
        select(ShiftResult, Shift)
        .join(Shift, Shift.id == ShiftResult.shift_id)
        .where(ShiftResult.seq > cursor)
        .order_by(ShiftResult.seq)
        .limit(limit)
    ).all()
    page = [(rev, shift) for rev, shift in rows]
    next_cursor = page[-1][0].seq if page else cursor
    return page, next_cursor


# ---------------------------------------------------------------- 现场停机对账（按设备）


def reconcile_device_downtime(
    db: Session,
    device_id: str,
    start: datetime,
    end: datetime,
    field_intervals: list[tuple[datetime, datetime]],
    seq: int | None,
) -> dict:
    """按设备对账现场停机记录与系统已结算归因。

    - 未指定 seq 时固定为请求开始时已提交的最大变更序号；seq 超过当前最大
      序号明确报错（不用当前结果替代）；
    - 取与半开窗口相交的班次中、截至 seq 已结算的班次（seq ≤ seq 有修订），
      每班选序号 ≤ seq 的最后修订；系统停机 = 这些修订固化归因明细（三类
      归因段）的并集，已结算覆盖 = 班次区间与窗口交集的并集；
    - 仅在已结算覆盖内比较现场/系统差异；现场落在覆盖外的分钟另计未对账；
    - 所选修订为明细结构引入前的历史修订（segments_available=FALSE，当时
      裁定状态无法可靠还原）时明确报错，不以当前状态推算。
    """
    get_device_or_404(db, device_id)
    win_start, win_end = _utc(start), _utc(end)
    max_committed_seq = _current_max_seq(db)
    if seq is None:
        pinned_seq = max_committed_seq  # 固定查询开始时的最新序号
    elif seq > max_committed_seq:
        raise ApiError(
            422,
            "INVALID_SEQ",
            f"reconciliation seq {seq} is beyond the latest committed change "
            f"seq {max_committed_seq}; omit seq to pin the current maximum "
            f"or specify a seq no greater than {max_committed_seq} (0 allowed)",
        )
    else:
        pinned_seq = seq

    # 每班次截至 pinned_seq 的最后修订（与班次快照同一选择规则，历史行不可变）
    last_rev_subq = (
        select(
            ShiftResult.shift_id.label("shift_id"),
            func.max(ShiftResult.revision).label("max_revision"),
        )
        .where(ShiftResult.seq <= pinned_seq)
        .group_by(ShiftResult.shift_id)
        .subquery()
    )
    rows = db.execute(
        select(Shift, ShiftResult)
        .join(ShiftResult, ShiftResult.shift_id == Shift.id)
        .join(
            last_rev_subq,
            and_(
                last_rev_subq.c.shift_id == ShiftResult.shift_id,
                last_rev_subq.c.max_revision == ShiftResult.revision,
            ),
        )
        .where(
            Shift.device_id == device_id,
            Shift.start_ts < win_end,
            Shift.end_ts > win_start,
        )
        .order_by(Shift.id)
    ).all()

    shift_views: list[reconciliation.ShiftCoverage] = []
    shift_refs: list[dict] = []
    for shift, rev in rows:
        if not rev.segments_available:
            raise ApiError(
                422,
                "SEGMENTS_UNAVAILABLE",
                f"shift id={shift.id} revision {rev.revision} (seq={rev.seq}) "
                "has no frozen result segments; the historical ruling state "
                "cannot be reliably reconstructed, so reconciliation at "
                f"seq={pinned_seq} is refused (current state is never substituted)",
            )
        seg_rows = (
            db.execute(
                select(ShiftResultSegment.start_ts, ShiftResultSegment.end_ts)
                .where(
                    ShiftResultSegment.shift_id == shift.id,
                    ShiftResultSegment.revision == rev.revision,
                )
                .order_by(ShiftResultSegment.seg_no)
            )
            .all()
        )
        shift_views.append(
            reconciliation.ShiftCoverage(
                shift_id=shift.id,
                revision=rev.revision,
                seq=rev.seq,
                coverage=(shift.start_ts, shift.end_ts),
                intervals=[(s, e) for s, e in seg_rows],
            )
        )
        shift_refs.append(
            {"shift_id": shift.id, "revision": rev.revision, "seq": rev.seq}
        )

    result = reconciliation.reconcile(
        win_start,
        win_end,
        [(_utc(s), _utc(e)) for s, e in field_intervals],
        shift_views,
    )
    return {
        "device_id": device_id,
        "start": start,
        "end": end,
        "seq": pinned_seq,
        "settled_minutes": result.settled_minutes,
        "field_only": result.field_only,
        "field_only_minutes": result.field_only_minutes,
        "system_only": result.system_only,
        "system_only_minutes": result.system_only_minutes,
        "unreconciled": result.unreconciled,
        "unreconciled_minutes": result.unreconciled_minutes,
        "reconciled_shifts": shift_refs,
    }


# ---------------------------------------------------------------- 班次归因快照

#: GET /devices/{id}/shift-snapshots 允许的页大小上限
MAX_SNAPSHOT_PAGE_SIZE = 500


def _current_max_seq(db: Session) -> int:
    """当前已提交的最大变更序号（无任何变更时为 0）。"""
    return db.execute(select(func.max(ShiftResult.seq))).scalar() or 0


def _snapshot_cursor_mac(payload: bytes) -> bytes:
    return hmac.new(settings.cursor_secret.encode(), payload, hashlib.sha256).digest()


def encode_snapshot_cursor(
    device_id: str,
    start: datetime,
    end: datetime,
    snapshot_seq: int,
    after_shift_id: int,
) -> str:
    """把续页状态编码为不透明游标（base64url(JSON) + HMAC-SHA256 签名）。"""
    body = {
        "device_id": device_id,
        "start": start.astimezone(timezone.utc).isoformat(),
        "end": end.astimezone(timezone.utc).isoformat(),
        "seq": snapshot_seq,
        "after": after_shift_id,
    }
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    mac = _snapshot_cursor_mac(payload)
    return (
        base64.urlsafe_b64encode(payload).rstrip(b"=")
        + b"."
        + base64.urlsafe_b64encode(mac).rstrip(b"=")
    ).decode()


def decode_snapshot_cursor(token: str) -> dict:
    """解码并验签续页游标；任何伪造/篡改/损坏一律视为非法游标。"""
    invalid = ApiError(
        422,
        "INVALID_CURSOR",
        "continuation cursor is invalid, malformed or tampered with; "
        "restart the snapshot from its first page without a cursor",
    )
    parts = token.split(".")
    if len(parts) != 2:
        raise invalid

    def _b64(seg: str) -> bytes:
        return seg.encode() + b"=" * (-len(seg) % 4)

    try:
        payload = base64.urlsafe_b64decode(_b64(parts[0]))
        mac = base64.urlsafe_b64decode(_b64(parts[1]))
        body = json.loads(payload)
    except (ValueError, json.JSONDecodeError):
        raise invalid
    if not hmac.compare_digest(mac, _snapshot_cursor_mac(payload)):
        raise invalid
    try:
        start = datetime.fromisoformat(body["start"])
        end = datetime.fromisoformat(body["end"])
        seq = int(body["seq"])
        after = int(body["after"])
        device_id = str(body["device_id"])
    except (KeyError, TypeError, ValueError):
        raise invalid
    if start.tzinfo is None or end.tzinfo is None or seq < 0 or after < 0:
        raise invalid
    return {"device_id": device_id, "start": start, "end": end, "seq": seq, "after": after}


def get_shift_snapshot(
    db: Session,
    device_id: str,
    start: datetime,
    end: datetime,
    snapshot_seq: int | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[tuple[Shift, ShiftResult]], int, str | None]:
    """按固定变更序号读取与窗口相交、截至该序号已结算班次的快照页。

    - 每班次只取 seq <= snapshot_seq 的最后修订（历史修订不可变）；
    - 按班次 id（即班次标识）稳定升序翻页，游标携带本页最后一个班次 id；
    - 返回 (页内 [(班次, 修订)], 快照序号, 续页游标或 None)。
    """
    get_device_or_404(db, device_id)
    win_start, win_end = _utc(start), _utc(end)
    max_committed_seq = _current_max_seq(db)

    if cursor is not None:
        state = decode_snapshot_cursor(cursor)
        # 游标必须与本次筛选条件及快照序号严格匹配，否则明确报错
        if state["device_id"] != device_id:
            raise ApiError(
                422,
                "INVALID_CURSOR",
                "continuation cursor belongs to a different device",
            )
        if state["start"] != win_start or state["end"] != win_end:
            raise ApiError(
                422,
                "INVALID_CURSOR",
                "continuation cursor does not match the requested time window; "
                "restart the snapshot with the original window",
            )
        if snapshot_seq is not None and state["seq"] != snapshot_seq:
            raise ApiError(
                422,
                "INVALID_CURSOR",
                "continuation cursor does not match the requested snapshot seq",
            )
        # 游标在首页签发时已校验过序号；历史序号永不失效，续页不再校验上界
        snapshot_seq = state["seq"]
        after_shift_id = state["after"]
    else:
        # 未指定快照序号：以本次请求读取时已提交的最大变更序号为准
        if snapshot_seq is None:
            snapshot_seq = max_committed_seq
        elif snapshot_seq > max_committed_seq:
            raise ApiError(
                422,
                "INVALID_SNAPSHOT_SEQ",
                f"snapshot seq {snapshot_seq} is beyond the latest committed "
                f"seq {max_committed_seq}; specify a seq no greater than the "
                "current maximum (0 is a valid empty snapshot)",
            )
        after_shift_id = 0

    # 每班次截至 snapshot_seq 的最后修订：窗口相交按半开区间判定（端点相接不相交）。
    # 快照只读不可变数据（shift_results/shift 已提交行不再改写），
    # 且按 shift 主键键集翻页 —— 后续更正/裁定/新结算只追加更大 seq 的修订，
    # 不会出现在 seq <= snapshot_seq 的结果中，同一快照后续页因此保持不变。
    last_rev_subq = (
        select(
            ShiftResult.shift_id.label("shift_id"),
            func.max(ShiftResult.revision).label("max_revision"),
        )
        .where(ShiftResult.seq <= snapshot_seq)
        .group_by(ShiftResult.shift_id)
        .subquery()
    )
    rows = db.execute(
        select(Shift, ShiftResult)
        .join(ShiftResult, ShiftResult.shift_id == Shift.id)
        .join(
            last_rev_subq,
            and_(
                last_rev_subq.c.shift_id == ShiftResult.shift_id,
                last_rev_subq.c.max_revision == ShiftResult.revision,
            ),
        )
        .where(
            Shift.device_id == device_id,
            Shift.start_ts < win_end,
            Shift.end_ts > win_start,
            Shift.id > after_shift_id,
        )
        .order_by(Shift.id)
        .limit(limit + 1)
    ).all()
    page = [(shift, rev) for shift, rev in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_snapshot_cursor(
            device_id, win_start, win_end, snapshot_seq, page[-1][0].id
        )
    return page, snapshot_seq, next_cursor


# ---------------------------------------------------------------- 停机损失费率发布


def publish_rate_version(
    db: Session, device_id: str, payload
) -> tuple[RateVersion, list[RateEntry], bool]:
    """为设备发布完整费率版本，或复用同内容的已发布版本。

    返回 (版本行, 费率行[按 entry_no], 是否幂等命中)。

    - 锁定设备行：并发发布串行，新版本号连续无缺口；同内容重试只返回原版本；
    - 金额解析、逐行区间与同原因重叠校验全部在事务内完成，任一非法整批
      失败（不留下版本与任何费率行）；版本与其全部行同事务提交；
    - 业务指纹只包含费率行（UTC 时间、原因、整数微元金额），请求列表顺序和
      等价十进制写法（"1" / "1.000000"）不影响指纹；note 不参与计价故不含；
    - 发布后不可变：不同费率内容之后的发布只追加新版本。
    """
    # 设备行锁：版本号在锁内按 max(version)+1 分配，串行化该设备的并发发布
    device = db.execute(
        select(Device).where(Device.id == device_id).with_for_update()
    ).scalar_one_or_none()
    if device is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"unknown device: {device_id!r}")

    lines: list[pricing.RateLine] = []
    for item in payload.entries:
        try:
            amount = pricing.parse_amount(item.amount_per_minute)
        except pricing.AmountError as exc:
            raise ApiError(422, "RATE_ENTRY_INVALID", str(exc))
        lines.append(
            pricing.RateLine(
                reason=item.reason,
                start=_utc(item.start),
                end=_utc(item.end),
                amount_per_minute=amount,
            )
        )
    try:
        pricing.validate_rate_lines(lines)
    except pricing.RateTableError as exc:
        raise ApiError(422, "RATE_ENTRY_INVALID", str(exc))

    content_hash = fp.rate_table_fingerprint(
        [(ln.start, ln.end, ln.reason, ln.amount_per_minute) for ln in lines]
    )
    existing = db.execute(
        select(RateVersion)
        .where(
            RateVersion.device_id == device_id,
            RateVersion.content_hash == content_hash,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        entries = list(
            db.execute(
                select(RateEntry)
                .where(
                    RateEntry.device_id == device_id,
                    RateEntry.version == existing.version,
                )
                .order_by(RateEntry.entry_no)
            )
            .scalars()
            .all()
        )
        db.commit()
        return existing, entries, True

    last_version = db.execute(
        select(func.max(RateVersion.version)).where(RateVersion.device_id == device_id)
    ).scalar() or 0
    version = RateVersion(
        device_id=device_id,
        version=last_version + 1,
        content_hash=content_hash,
        note=payload.note or "",
    )
    db.add(version)
    db.flush()  # 会话 autoflush=False：先落版本行，费率行复合外键才可见
    # entry_no 按起点（再按原因、终点）稳定排序后编号；发布内容即冻结内容
    ordered = sorted(lines, key=lambda ln: (ln.start, ln.reason, ln.end))
    next_version = last_version + 1
    entries: list[RateEntry] = []
    for entry_no, line in enumerate(ordered, start=1):
        entry = RateEntry(
            device_id=device_id,
            version=next_version,
            entry_no=entry_no,
            start_ts=line.start,
            end_ts=line.end,
            reason=line.reason,
            amount_per_minute=line.amount_per_minute,
        )
        db.add(entry)
        entries.append(entry)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.execute(
            select(RateVersion).where(
                RateVersion.device_id == device_id,
                RateVersion.content_hash == content_hash,
            )
        ).scalar_one_or_none()
        if winner is not None:
            entries = list(
                db.execute(
                    select(RateEntry)
                    .where(
                        RateEntry.device_id == device_id,
                        RateEntry.version == winner.version,
                    )
                    .order_by(RateEntry.entry_no)
                )
                .scalars()
                .all()
            )
            return winner, entries, True
        # 排他约束等兜底（应用层校验已覆盖）
        raise ApiError(
            422,
            "RATE_ENTRY_INVALID",
            "rate table rejected: overlapping or illegal rate entries",
        )
    db.refresh(version)
    return version, entries, False


def get_rate_version(
    db: Session, device_id: str, version: int
) -> tuple[RateVersion, list[RateEntry]]:
    """读取设备某版本的完整费率表；未知设备/版本分别 404。"""
    get_device_or_404(db, device_id)
    rv = db.execute(
        select(RateVersion).where(
            RateVersion.device_id == device_id, RateVersion.version == version
        )
    ).scalar_one_or_none()
    if rv is None:
        raise ApiError(
            404,
            "RATE_VERSION_NOT_FOUND",
            f"device {device_id!r} has no rate version {version}",
        )
    entries = list(
        db.execute(
            select(RateEntry)
            .where(
                RateEntry.device_id == device_id, RateEntry.version == version
            )
            .order_by(RateEntry.entry_no)
        )
        .scalars()
        .all()
    )
    return rv, entries


def list_rate_versions(db: Session, device_id: str) -> list[tuple[RateVersion, int]]:
    """列出设备的全部费率版本（版本号降序，最新在前）及各版本费率行数。"""
    get_device_or_404(db, device_id)
    rows = (
        db.execute(
            select(RateVersion, func.count(RateEntry.id))
            .outerjoin(
                RateEntry,
                and_(
                    RateEntry.device_id == RateVersion.device_id,
                    RateEntry.version == RateVersion.version,
                ),
            )
            .where(RateVersion.device_id == device_id)
            .group_by(RateVersion.id, RateVersion.device_id, RateVersion.version)
            .order_by(RateVersion.version.desc())
        )
        .all()
    )
    return [(rv, int(count)) for rv, count in rows]


# ---------------------------------------------------------------- 停机损失核算


def _frozen_segments(
    db: Session, shift_id: int, rev: ShiftResult
) -> list[ShiftResultSegment]:
    return list(
        db.execute(
            select(ShiftResultSegment)
            .where(
                ShiftResultSegment.shift_id == shift_id,
                ShiftResultSegment.revision == rev.revision,
            )
            .order_by(ShiftResultSegment.seg_no)
        )
        .scalars()
        .all()
    )


def _get_or_create_accounting(
    db: Session,
    shift: Shift,
    rev: ShiftResult,
    rate_version_no: int,
) -> tuple[LossAccounting, list[LossAccountingPiece], bool]:
    """在调用方事务内形成或复用 (班次, 修订, 费率版本) 的损失账，不提交事务。

    调用方须已锁定班次行（或本来就在班次行锁内）。本函数：
    - 命中同组合既有账 → 原样返回（不写入），idempotent=True；
    - 修订无固化明细（结构引入前的历史修订）→ SEGMENTS_UNAVAILABLE；
    - 锁定该设备的费率版本行并计价，写入账头与全部切分明细后 flush（不 commit），
      调用方事务统一提交/回滚，保证"整单同事务固化"。
    """
    shift_id = shift.id
    # 幂等：同 (班次, 修订, 费率版本) 只固化一次，历史账原样返回不可改写
    existing = db.execute(
        select(LossAccounting).where(
            LossAccounting.shift_id == shift_id,
            LossAccounting.revision == rev.revision,
            LossAccounting.rate_version == rate_version_no,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, _accounting_pieces(db, existing.id), True

    if not rev.segments_available:
        raise ApiError(
            422,
            "SEGMENTS_UNAVAILABLE",
            f"shift id={shift_id} revision {rev.revision} has no frozen result "
            "segments (a historical revision from before segments were recorded); "
            "loss accounting is refused and current state is never substituted",
        )

    # 锁定费率版本行（必须属于班次所属设备；历史版本不可变）
    rv = db.execute(
        select(RateVersion)
        .where(
            RateVersion.device_id == shift.device_id,
            RateVersion.version == rate_version_no,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if rv is None:
        raise ApiError(
            404,
            "RATE_VERSION_NOT_FOUND",
            f"device {shift.device_id!r} has no rate version {rate_version_no}",
        )

    rate_rows = (
        db.execute(
            select(RateEntry)
            .where(
                RateEntry.device_id == shift.device_id,
                RateEntry.version == rate_version_no,
            )
            .order_by(RateEntry.entry_no)
        )
        .scalars()
        .all()
    )
    lines = [
        pricing.RateLine(
            reason=r.reason,
            start=r.start_ts,
            end=r.end_ts,
            amount_per_minute=r.amount_per_minute,
        )
        for r in rate_rows
    ]
    seg_rows = _frozen_segments(db, shift_id, rev)
    frozen = [
        pricing.FrozenSegment(
            seg_no=s.seg_no, start=s.start_ts, end=s.end_ts, reason=s.reason
        )
        for s in seg_rows
    ]
    result = pricing.price_segments(frozen, lines)

    accounting = LossAccounting(
        shift_id=shift_id,
        device_id=shift.device_id,
        revision=rev.revision,
        rate_version=rate_version_no,
        total_cents=result.total_cents,
        priced_minutes=result.priced_minutes,
        unpriced_minutes=result.unpriced_minutes,
        by_reason={
            reason: {
                "priced_minutes": rt.priced_minutes,
                "amount_cents": rt.amount_cents,
                "unpriced_minutes": rt.unpriced_minutes,
            }
            for reason, rt in result.by_reason.items()
        },
    )
    db.add(accounting)
    db.flush()  # 取得 accounting.id；唯一约束兜底并发同组合写入

    pieces: list[LossAccountingPiece] = []
    # 切分明细按时间顺序统一编号：定价段与待定价区间合并排序
    ordered_pieces = sorted(
        [("p", p) for p in result.priced]
        + [("u", u) for u in result.unpriced],
        key=lambda x: (x[1].start, 0 if x[0] == "p" else 1),
    )
    for piece_no, (kind, item) in enumerate(ordered_pieces, start=1):
        if kind == "p":
            pieces.append(
                LossAccountingPiece(
                    accounting_id=accounting.id,
                    piece_no=piece_no,
                    seg_no=item.seg_no,
                    start_ts=item.start,
                    end_ts=item.end,
                    reason=item.reason,
                    minutes=item.minutes,
                    priced=True,
                    rate_per_minute=item.rate_per_minute_micro,
                    amount_cents=item.amount_cents,
                )
            )
        else:
            pieces.append(
                LossAccountingPiece(
                    accounting_id=accounting.id,
                    piece_no=piece_no,
                    seg_no=item.seg_no,
                    start_ts=item.start,
                    end_ts=item.end,
                    reason=item.reason,
                    minutes=item.minutes,
                    priced=False,
                    rate_per_minute=None,
                    amount_cents=None,
                )
            )
    for piece in pieces:
        db.add(piece)
    db.flush()
    return accounting, pieces, False


def account_shift_loss(
    db: Session, shift_id: int, rate_version_no: int, revision: int | None
) -> tuple[LossAccounting, list[LossAccountingPiece], bool]:
    """对已结算班次按 (修订, 费率版本) 核算停机损失并幂等固化。

    返回 (账头, 全部切分明细[按 piece_no], 是否幂等命中既有账)。
    - 班次行 FOR UPDATE：同一班次的并发核算相互串行，同组合只有一次写入；
    - 费率版本行 FOR UPDATE：只可能锁定一个已完整提交的版本；版本发布整批
      原子、历史版本不可变，锁不阻塞新版本发布，但保证本次核算全程引用
      同一完整版本，不会读到发布中的半成品；
    - 修订无固化明细（结构引入前的历史修订）明确拒绝，绝不以当前状态推算；
    - 账与明细同事务写入；历史账不可改写，后续发布/更正只产生新组合的账。
    """
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id).with_for_update()
    ).scalar_one_or_none()
    if shift is None:
        raise ApiError(404, "SHIFT_NOT_FOUND", f"unknown shift id={shift_id}")

    latest = _latest_revision(db, shift_id)
    if latest is None:
        raise ApiError(
            404, "RESULT_NOT_FOUND", f"shift id={shift_id} has not been settled"
        )
    if revision is None:
        rev = latest
    else:
        rev = db.execute(
            select(ShiftResult).where(
                ShiftResult.shift_id == shift_id, ShiftResult.revision == revision
            )
        ).scalar_one_or_none()
        if rev is None:
            raise ApiError(
                404,
                "REVISION_NOT_FOUND",
                f"shift id={shift_id} has no revision {revision}",
            )

    try:
        accounting, pieces, idempotent = _get_or_create_accounting(
            db, shift, rev, rate_version_no
        )
    except ApiError:
        db.rollback()  # 显式校验失败：释放班次行锁
        raise
    try:
        db.commit()
    except IntegrityError:  # 并发同组合核算：唯一约束保证只有一个固化账
        db.rollback()
        winner = db.execute(
            select(LossAccounting).where(
                LossAccounting.shift_id == shift_id,
                LossAccounting.revision == rev.revision,
                LossAccounting.rate_version == rate_version_no,
            )
        ).scalar_one()
        return winner, _accounting_pieces(db, winner.id), True
    db.refresh(accounting)
    return accounting, pieces, idempotent


def _accounting_pieces(db: Session, accounting_id: int) -> list[LossAccountingPiece]:
    return list(
        db.execute(
            select(LossAccountingPiece)
            .where(LossAccountingPiece.accounting_id == accounting_id)
            .order_by(LossAccountingPiece.piece_no)
        )
        .scalars()
        .all()
    )


def list_loss_accountings(
    db: Session, shift_id: int, revision: int | None, rate_version: int | None
) -> tuple[Shift, list[tuple[LossAccounting, list[LossAccountingPiece]]]]:
    """列出班次已固化的损失账（可按修订/费率版本过滤），按费率版本升序。

    未知班次 404；过滤条件不匹配任何已固化账时返回空列表（不报错）。
    """
    shift = get_shift_or_404(db, shift_id)
    stmt = select(LossAccounting).where(LossAccounting.shift_id == shift_id)
    if revision is not None:
        stmt = stmt.where(LossAccounting.revision == revision)
    if rate_version is not None:
        stmt = stmt.where(LossAccounting.rate_version == rate_version)
    stmt = stmt.order_by(LossAccounting.rate_version, LossAccounting.revision)
    rows = list(db.execute(stmt).scalars().all())
    out: list[tuple[LossAccounting, list[LossAccountingPiece]]] = []
    for accounting in rows:
        out.append((accounting, _accounting_pieces(db, accounting.id)))
    return shift, out


# ------------------------------------------------ 停机损失按生产批次分摊


def _allocation_content_hash(
    accounting_ids: list[int], batches: list[tuple[str, datetime, datetime]]
) -> str:
    """规范化提交内容 → sha256（与列表顺序无关）。"""
    return fp.allocation_fingerprint(accounting_ids, batches)


def allocate_loss_costs(db: Session, payload) -> tuple[
    LossCostAllocation,
    list[
        tuple[
            LossAccounting,
            LossCostAllocationAccount,
            list[LossCostAllocationSplit],
        ]
    ],
    bool,
]:
    """按生产批次窗口把同一设备的已固化损失账分摊到各批次与"未分配"并固化。

    返回 (分摊单头, [(原损失账头, 单内账引用行, 该账全部来源分摊行[按 split_no])],
    是否幂等)。
    任一非法（未知设备/账、跨设备账、同班次多账、批次号重复/窗口重叠/非法窗口）
    整单拒绝，事务回滚，不留部分结果。同单号同内容重试返回原结果；同单号异内容
    409 ALLOCATION_CONFLICT。
    """
    device = get_device_or_404(db, payload.device_id)

    # 规范化并校验批次窗口（应用层给出语义化错误码；schema 已保证整分钟/非空）
    windows = [
        allocation.BatchWindow(batch_no=b.batch_no, start=_utc(b.start), end=_utc(b.end))
        for b in payload.batches
    ]
    try:
        allocation.validate_windows(windows)
    except allocation.AllocationError as exc:
        raise ApiError(422, "BATCH_WINDOW_INVALID", str(exc))

    accounting_ids = list(payload.accounting_ids)
    content_hash = _allocation_content_hash(
        accounting_ids, [(b.batch_no, b.start, b.end) for b in payload.batches]
    )

    # 单号行锁：同单号的并发提交串行；先查既有单（同内容幂等 / 异内容冲突）
    existing = db.execute(
        select(LossCostAllocation)
        .where(LossCostAllocation.allocation_no == payload.allocation_no)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash != content_hash:
            raise ApiError(
                409,
                "ALLOCATION_CONFLICT",
                f"allocation_no {payload.allocation_no!r} already exists with "
                "different content",
            )
        rows = _allocation_account_rows(db, existing.id)
        db.commit()  # 释放单号行锁
        return existing, rows, True

    # 锁引用的损失账行（id 升序，避免死锁）：账不可变，加锁主要与跨单引用及
    # 潜在清理串行；读取内容与后续写入在同一事务快照内完成。
    account_rows: list[LossAccounting] = []
    for accounting_id in sorted(set(accounting_ids)):
        acc = db.execute(
            select(LossAccounting)
            .where(LossAccounting.id == accounting_id)
            .with_for_update()
        ).scalar_one_or_none()
        if acc is None or acc.device_id != device.id:
            raise ApiError(
                404,
                "LOSS_ACCOUNTING_NOT_FOUND",
                f"device {device.id!r} has no loss accounting id={accounting_id}",
            )
        account_rows.append(acc)

    # 每个班次最多选一份账
    shift_seen: set[int] = set()
    for acc in account_rows:
        if acc.shift_id in shift_seen:
            raise ApiError(
                422,
                "ACCOUNTINGS_SAME_SHIFT",
                f"at most one loss accounting per shift may be allocated in an "
                f"order; shift id={acc.shift_id} appears more than once",
            )
        shift_seen.add(acc.shift_id)

    head = LossCostAllocation(
        allocation_no=payload.allocation_no,
        device_id=device.id,
        content_hash=content_hash,
        batches=[
            {
                "batch_no": b.batch_no,
                "start_ts": b.start.isoformat(),
                "end_ts": b.end.isoformat(),
            }
            for b in sorted(windows, key=lambda w: w.batch_no)
        ],
        accounting_ids=sorted(acc.id for acc in account_rows),
    )
    db.add(head)
    db.flush()  # 取得 head.id

    split_no = 0
    out_rows: list[
        tuple[
            LossAccounting,
            LossCostAllocationAccount,
            list[LossCostAllocationSplit],
        ]
    ] = []
    for acc in sorted(account_rows, key=lambda a: a.id):
        piece_rows = _accounting_pieces(db, acc.id)
        pieces = [
            allocation.Piece(
                piece_no=p.piece_no,
                seg_no=p.seg_no,
                start=p.start_ts,
                end=p.end_ts,
                reason=p.reason,
                minutes=p.minutes,
                priced=p.priced,
                amount_cents=p.amount_cents,
            )
            for p in piece_rows
        ]
        result = allocation.allocate_pieces(pieces, windows)
        ordered_splits = sorted(
            result.splits,
            key=lambda s: (s.start, s.end, s.source_piece_no, s.dest, s.batch_no or ""),
        )

        alloc_acc = LossCostAllocationAccount(
            allocation_id=head.id,
            accounting_id=acc.id,
            shift_id=acc.shift_id,
            revision=acc.revision,
            rate_version=acc.rate_version,
            total_cents=acc.total_cents,
            priced_minutes=acc.priced_minutes,
            unpriced_minutes=acc.unpriced_minutes,
        )
        db.add(alloc_acc)
        db.flush()  # 取得 alloc_acc.id 供外键

        stored: list[LossCostAllocationSplit] = []
        for sp in ordered_splits:
            split_no += 1
            row = LossCostAllocationSplit(
                allocation_id=head.id,
                alloc_account_id=alloc_acc.id,
                accounting_id=acc.id,
                split_no=split_no,
                source_piece_no=sp.source_piece_no,
                seg_no=sp.seg_no,
                dest=sp.dest,
                batch_no=sp.batch_no,
                start_ts=sp.start,
                end_ts=sp.end,
                reason=sp.reason,
                minutes=sp.minutes,
                priced=sp.priced,
                amount_cents=sp.amount_cents,
            )
            db.add(row)
            stored.append(row)
        out_rows.append((acc, alloc_acc, stored))

    try:
        db.commit()
    except IntegrityError:
        # 并发同单号提交：唯一约束保证只有一单；竞争方转幂等读取
        db.rollback()
        winner = db.execute(
            select(LossCostAllocation).where(
                LossCostAllocation.allocation_no == payload.allocation_no
            )
        ).scalar_one()
        if winner.content_hash != content_hash:
            raise ApiError(
                409,
                "ALLOCATION_CONFLICT",
                f"allocation_no {payload.allocation_no!r} already exists with "
                "different content",
            )
        return winner, _allocation_account_rows(db, winner.id), True

    db.refresh(head)
    return head, out_rows, False


def _allocation_account_rows(
    db: Session, allocation_id: int
) -> list[
    tuple[
        LossAccounting,
        LossCostAllocationAccount,
        list[LossCostAllocationSplit],
    ]
]:
    """读取分摊单内各账引用及其全部来源分摊行（账按 accounting_id，行按 split_no）。

    同时回连原损失账头（不可变）以取得按原因合计等账面信息。
    """
    accs = list(
        db.execute(
            select(LossCostAllocationAccount)
            .where(LossCostAllocationAccount.allocation_id == allocation_id)
            .order_by(LossCostAllocationAccount.accounting_id)
        )
        .scalars()
        .all()
    )
    out: list[
        tuple[
            LossAccounting,
            LossCostAllocationAccount,
            list[LossCostAllocationSplit],
        ]
    ] = []
    for aa in accs:
        original = db.execute(
            select(LossAccounting).where(LossAccounting.id == aa.accounting_id)
        ).scalar_one()
        splits = list(
            db.execute(
                select(LossCostAllocationSplit)
                .where(LossCostAllocationSplit.alloc_account_id == aa.id)
                .order_by(LossCostAllocationSplit.split_no)
            )
            .scalars()
            .all()
        )
        out.append((original, aa, splits))
    return out


def get_loss_cost_allocation(
    db: Session, allocation_no: str
) -> tuple[
    LossCostAllocation,
    list[
        tuple[
            LossAccounting,
            LossCostAllocationAccount,
            list[LossCostAllocationSplit],
        ]
    ],
]:
    """按单号读取固化的分摊结果；未知单号 404 ALLOCATION_NOT_FOUND。"""
    head = db.execute(
        select(LossCostAllocation).where(
            LossCostAllocation.allocation_no == allocation_no
        )
    ).scalar_one_or_none()
    if head is None:
        raise ApiError(
            404,
            "ALLOCATION_NOT_FOUND",
            f"unknown allocation_no: {allocation_no!r}",
        )
    return head, _allocation_account_rows(db, head.id)


def list_device_loss_cost_allocations(
    db: Session, device_id: str
) -> list[tuple[
    LossCostAllocation,
    list[
        tuple[
            LossAccounting,
            LossCostAllocationAccount,
            list[LossCostAllocationSplit],
        ]
    ],
]]:
    """列出设备的全部固化分摊单（按 id 升序）；未知设备 404。"""
    get_device_or_404(db, device_id)
    heads = list(
        db.execute(
            select(LossCostAllocation)
            .where(LossCostAllocation.device_id == device_id)
            .order_by(LossCostAllocation.id)
        )
        .scalars()
        .all()
    )
    return [(h, _allocation_account_rows(db, h.id)) for h in heads]


# ------------------------------------------------ 按设备期间停机损失结账


def _closing_content_hash(
    device_id: str,
    start: datetime,
    end: datetime,
    seq: int,
    rate_version: int,
) -> str:
    """规范化结账输入 → sha256（时间归一 UTC 整分钟 ISO 串）。"""
    return fp.closing_fingerprint(device_id, start, end, seq, rate_version)


def _closing_items(
    db: Session, closing_id: int
) -> list[
    tuple[
        LossPeriodClosingItem,
        Shift,
        LossAccounting,
        list[LossAccountingPiece],
    ]
]:
    """读取结账内各班次条目及班次区间、引用的损失账（条目按 item_no，明细按 piece_no）。"""
    items = list(
        db.execute(
            select(LossPeriodClosingItem)
            .where(LossPeriodClosingItem.closing_id == closing_id)
            .order_by(LossPeriodClosingItem.item_no)
        )
        .scalars()
        .all()
    )
    out: list[
        tuple[
            LossPeriodClosingItem,
            Shift,
            LossAccounting,
            list[LossAccountingPiece],
        ]
    ] = []
    for item in items:
        shift = db.execute(
            select(Shift).where(Shift.id == item.shift_id)
        ).scalar_one()
        accounting = db.execute(
            select(LossAccounting).where(LossAccounting.id == item.accounting_id)
        ).scalar_one()
        pieces = _accounting_pieces(db, accounting.id)
        out.append((item, shift, accounting, pieces))
    return out


def close_device_loss_period(
    db: Session, payload
) -> tuple[
    LossPeriodClosing,
    list[
        tuple[
            LossPeriodClosingItem,
            Shift,
            LossAccounting,
            list[LossAccountingPiece],
        ]
    ],
    bool,
]:
    """按设备封存一个期间的停机损失结账（整单同事务，全或无）。

    返回 (结账头, [(条目, 引用损失账头, 账切分明细[按 piece_no])], 是否幂等)。

    - 只纳入**期间内起始**（班次 start_ts ∈ [start, end)）且截至 seq 已有结算
      修订的班次，每班取 seq <= 截止序号的最后修订（与班次快照/对账同一选择
      规则，历史修订不可变）；
    - 任一选中修订缺少固化归因段（segments_available=FALSE）→ 422
      SEGMENTS_UNAVAILABLE 整单失败，不以当前状态推算；
    - 在同一事务内按班次 id 升序锁定各班次行，沿用现有逐段计价与待定价规则
      形成或复用 (班次, 修订, 费率版本) 的损失账（复用既有账，不重算改写），
      再把各班次所选修订、账引用与按原因汇总同事务固化；
    - 无符合班次时固化一张空结账（条目为空、金额/分钟全 0）；
    - 同单号同输入重试返回原结账（200，idempotent=true）；同单号异输入
      409 CLOSING_CONFLICT；序号越界 422 INVALID_SEQ、未知设备 404、
      该设备未知费率版本 404 RATE_VERSION_NOT_FOUND。
    """
    device = get_device_or_404(db, payload.device_id)
    win_start, win_end = _utc(payload.start), _utc(payload.end)

    content_hash = _closing_content_hash(
        device.id, win_start, win_end, payload.seq, payload.rate_version
    )

    # 单号行锁：同单号的并发提交串行（先查既有单：同输入幂等 / 异输入冲突）。
    # 单号全局唯一，该锁也让并发结账整体串行化，避免并发期锁顺序竞争。
    existing = db.execute(
        select(LossPeriodClosing)
        .where(LossPeriodClosing.closing_no == payload.closing_no)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash != content_hash:
            raise ApiError(
                409,
                "CLOSING_CONFLICT",
                f"closing_no {payload.closing_no!r} already exists with different "
                "input (device/period/seq/rate_version)",
            )
        db.commit()  # 释放单号行锁
        return existing, _closing_items(db, existing.id), True

    # 截止序号必须不越界（序号为必填；1..当前已提交最大序号；seq=0 时无结算）
    max_committed_seq = _current_max_seq(db)
    if payload.seq > max_committed_seq:
        raise ApiError(
            422,
            "INVALID_SEQ",
            f"closing seq {payload.seq} is beyond the latest committed change "
            f"seq {max_committed_seq}; specify a seq no greater than "
            f"{max_committed_seq} (0 yields an empty closing)",
        )

    # 费率版本必须属于该设备且已完整发布（版本行不可变；不提前加锁，
    # 逐班次计价在费率版本行锁内再读同一完整版本）
    rv = db.execute(
        select(RateVersion).where(
            RateVersion.device_id == device.id,
            RateVersion.version == payload.rate_version,
        )
    ).scalar_one_or_none()
    if rv is None:
        raise ApiError(
            404,
            "RATE_VERSION_NOT_FOUND",
            f"device {device.id!r} has no rate version {payload.rate_version}",
        )

    # 每班次截至 seq 的最后修订（与班次快照/对账同一选择规则，历史行不可变）；
    # 仅纳入期间内起始的班次：shift.start_ts ∈ [win_start, win_end)（半开）。
    last_rev_subq = (
        select(
            ShiftResult.shift_id.label("shift_id"),
            func.max(ShiftResult.revision).label("max_revision"),
        )
        .where(ShiftResult.seq <= payload.seq)
        .group_by(ShiftResult.shift_id)
        .subquery()
    )
    rows = db.execute(
        select(Shift, ShiftResult)
        .join(ShiftResult, ShiftResult.shift_id == Shift.id)
        .join(
            last_rev_subq,
            and_(
                last_rev_subq.c.shift_id == ShiftResult.shift_id,
                last_rev_subq.c.max_revision == ShiftResult.revision,
            ),
        )
        .where(
            Shift.device_id == device.id,
            Shift.start_ts >= win_start,
            Shift.start_ts < win_end,
        )
        .order_by(Shift.id)
    ).all()

    # 先整单校验：任一修订无固化明细 → 整单失败（此时尚未写入任何新账/结账行）
    for shift, rev in rows:
        if not rev.segments_available:
            raise ApiError(
                422,
                "SEGMENTS_UNAVAILABLE",
                f"shift id={shift.id} revision {rev.revision} (seq={rev.seq}) "
                "has no frozen result segments; the historical ruling state "
                "cannot be reliably reconstructed, so the period closing is "
                "refused (current state is never substituted)",
            )

    head = LossPeriodClosing(
        closing_no=payload.closing_no,
        device_id=device.id,
        content_hash=content_hash,
        start_ts=win_start,
        end_ts=win_end,
        seq=payload.seq,
        rate_version=payload.rate_version,
        total_cents=0,
        priced_minutes=0,
        unpriced_minutes=0,
        by_reason={},
    )
    try:
        db.add(head)
        db.flush()  # 取得 head.id；单号唯一约束在此即可能拒绝并发同单号

        account_summaries: list[closing.AccountSummary] = []
        item_rows: list[
            tuple[
                LossPeriodClosingItem,
                Shift,
                LossAccounting,
                list[LossAccountingPiece],
            ]
        ] = []
        # 按班次 id 升序处理：与损失核算/分摊的加锁次序一致，避免死锁
        for item_no, (shift, rev) in enumerate(rows, start=1):
            locked = db.execute(
                select(Shift).where(Shift.id == shift.id).with_for_update()
            ).scalar_one()
            accounting, pieces, _idempotent = _get_or_create_accounting(
                db, locked, rev, payload.rate_version
            )
            account_summaries.append(
                closing.AccountSummary(
                    total_cents=accounting.total_cents,
                    priced_minutes=accounting.priced_minutes,
                    unpriced_minutes=accounting.unpriced_minutes,
                    by_reason=accounting.by_reason,
                )
            )
            item = LossPeriodClosingItem(
                closing_id=head.id,
                item_no=item_no,
                shift_id=shift.id,
                revision=rev.revision,
                revision_seq=rev.seq,
                accounting_id=accounting.id,
                total_cents=accounting.total_cents,
                priced_minutes=accounting.priced_minutes,
                unpriced_minutes=accounting.unpriced_minutes,
                by_reason=accounting.by_reason,
            )
            db.add(item)
            item_rows.append((item, locked, accounting, pieces))

        totals = closing.summarize(account_summaries)
        head.total_cents = totals.total_cents
        head.priced_minutes = totals.priced_minutes
        head.unpriced_minutes = totals.unpriced_minutes
        head.by_reason = {
            reason: {
                "priced_minutes": rt.priced_minutes,
                "amount_cents": rt.amount_cents,
                "unpriced_minutes": rt.unpriced_minutes,
            }
            for reason, rt in totals.by_reason.items()
        }
        db.commit()
    except ApiError:
        db.rollback()  # 业务校验失败（如计价阶段异常）：整体回滚不留痕
        raise
    except IntegrityError:
        # 并发同单号提交：单号唯一约束保证只有一单；竞争方转幂等读取/冲突。
        # 同事务已写入的新损失账随回滚撤销——若赢家已固化相同组合的账，
        # 下次重试/读取直接复用；不留下孤儿账或半截结账。
        db.rollback()
        winner = db.execute(
            select(LossPeriodClosing).where(
                LossPeriodClosing.closing_no == payload.closing_no
            )
        ).scalar_one_or_none()
        if winner is None:
            raise  # 非单号冲突的其他约束异常：交由上层处理
        if winner.content_hash != content_hash:
            raise ApiError(
                409,
                "CLOSING_CONFLICT",
                f"closing_no {payload.closing_no!r} already exists with different "
                "input (device/period/seq/rate_version)",
            )
        return winner, _closing_items(db, winner.id), True
    db.refresh(head)
    return head, item_rows, False


def get_loss_period_closing(
    db: Session, closing_no: str
) -> tuple[
    LossPeriodClosing,
    list[
        tuple[
            LossPeriodClosingItem,
            Shift,
            LossAccounting,
            list[LossAccountingPiece],
        ]
    ],
]:
    """按单号读取固化的期间停机损失结账；未知单号 404 CLOSING_NOT_FOUND。"""
    head = db.execute(
        select(LossPeriodClosing).where(LossPeriodClosing.closing_no == closing_no)
    ).scalar_one_or_none()
    if head is None:
        raise ApiError(
            404,
            "CLOSING_NOT_FOUND",
            f"unknown closing_no: {closing_no!r}",
        )
    return head, _closing_items(db, head.id)


def list_device_loss_period_closings(
    db: Session, device_id: str
) -> list[
    tuple[
        LossPeriodClosing,
        list[
            tuple[
                LossPeriodClosingItem,
                Shift,
                LossAccounting,
                list[LossAccountingPiece],
            ]
        ],
    ]
]:
    """列出设备的全部固化期间结账（按 id 升序）；未知设备 404。"""
    get_device_or_404(db, device_id)
    heads = list(
        db.execute(
            select(LossPeriodClosing)
            .where(LossPeriodClosing.device_id == device_id)
            .order_by(LossPeriodClosing.id)
        )
        .scalars()
        .all()
    )
    return [(h, _closing_items(db, h.id)) for h in heads]


# ------------------------------------------------ 计划保养提交与核对


def _maintenance_plan_hash(
    device_id: str,
    start: datetime,
    end: datetime,
    windows: list[tuple[datetime, datetime]],
) -> str:
    """规范化计划内容 → sha256（时间归一 UTC 整分钟 ISO 串，窗口顺序无关）。"""
    return fp.maintenance_plan_fingerprint(device_id, start, end, windows)


def submit_maintenance_plan(
    db: Session, payload
) -> tuple[MaintenancePlan, bool]:
    """提交计划保养单（提交后不可变；同单号同内容幂等，异内容冲突）。

    返回 (计划单, 是否幂等命中)。
    - 窗口非空、完整落在期间内、互不重叠；同设备各计划单期间不得重叠
      （半开，端点相接允许）——任一非法整单拒绝，不留痕；
    - 同单号同内容重试返回原单（200，idempotent=True，窗口换序、等价时区
      仍算同内容）；同单号异内容 409 MAINTENANCE_PLAN_CONFLICT；
    - 与同设备既有计划期间重叠 409 MAINTENANCE_PLAN_OVERLAP；
    - 未知设备 404 DEVICE_NOT_FOUND。
    """
    get_device_or_404(db, payload.device_id)
    win_start, win_end = _utc(payload.start), _utc(payload.end)
    raw_windows = [(_utc(w.start), _utc(w.end)) for w in payload.windows]
    try:
        norm_windows = maintenance.normalize_windows(
            (win_start, win_end), raw_windows
        )
    except maintenance.MaintenancePlanError as exc:
        raise ApiError(422, "MAINTENANCE_WINDOW_INVALID", str(exc))

    content_hash = _maintenance_plan_hash(
        payload.device_id, win_start, win_end, raw_windows
    )

    # 单号行锁：同单号的并发提交串行（先查既有单：同内容幂等 / 异内容冲突）
    existing = db.execute(
        select(MaintenancePlan)
        .where(MaintenancePlan.plan_no == payload.plan_no)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content_hash != content_hash:
            raise ApiError(
                409,
                "MAINTENANCE_PLAN_CONFLICT",
                f"plan_no {payload.plan_no!r} already exists with different content",
            )
        db.commit()  # 释放单号行锁
        return existing, True

    # 同设备期间重叠（半开判定：start < other.end AND end > other.start）
    conflict = (
        db.execute(
            select(MaintenancePlan).where(
                MaintenancePlan.device_id == payload.device_id,
                MaintenancePlan.start_ts < win_end,
                MaintenancePlan.end_ts > win_start,
            )
        )
        .scalars()
        .first()
    )
    if conflict is not None:
        raise ApiError(
            409,
            "MAINTENANCE_PLAN_OVERLAP",
            f"plan period [{win_start.isoformat()}, {win_end.isoformat()}) overlaps "
            f"existing plan {conflict.plan_no!r} "
            f"[{conflict.start_ts.isoformat()}, {conflict.end_ts.isoformat()}) "
            f"on device {payload.device_id!r}",
        )

    plan = MaintenancePlan(
        plan_no=payload.plan_no,
        device_id=payload.device_id,
        content_hash=content_hash,
        start_ts=win_start,
        end_ts=win_end,
        windows=[
            {"start_ts": s.isoformat(), "end_ts": e.isoformat()}
            for s, e in norm_windows
        ],
    )
    db.add(plan)
    try:
        db.commit()
    except IntegrityError:
        # 单号唯一 / 期间排他约束兜底并发竞争：整体回滚后重新判定
        db.rollback()
        winner = db.execute(
            select(MaintenancePlan).where(
                MaintenancePlan.plan_no == payload.plan_no
            )
        ).scalar_one_or_none()
        if winner is not None:
            if winner.content_hash != content_hash:
                raise ApiError(
                    409,
                    "MAINTENANCE_PLAN_CONFLICT",
                    f"plan_no {payload.plan_no!r} already exists with different content",
                )
            return winner, True
        # 单号不同：并发提交的同设备重叠单先提交
        raise ApiError(
            409,
            "MAINTENANCE_PLAN_OVERLAP",
            "plan period overlaps a concurrently submitted plan on this device",
        )
    db.refresh(plan)
    return plan, False


def get_maintenance_plan(db: Session, plan_no: str) -> MaintenancePlan:
    plan = db.execute(
        select(MaintenancePlan).where(MaintenancePlan.plan_no == plan_no)
    ).scalar_one_or_none()
    if plan is None:
        raise ApiError(
            404, "MAINTENANCE_PLAN_NOT_FOUND", f"unknown plan_no: {plan_no!r}"
        )
    return plan


def list_device_maintenance_plans(
    db: Session, device_id: str
) -> list[MaintenancePlan]:
    """列出设备的全部计划保养单（按提交先后；未知设备 404）。"""
    get_device_or_404(db, device_id)
    return list(
        db.execute(
            select(MaintenancePlan)
            .where(MaintenancePlan.device_id == device_id)
            .order_by(MaintenancePlan.id)
        )
        .scalars()
        .all()
    )


def _plan_windows(plan: MaintenancePlan) -> list[tuple[datetime, datetime]]:
    return [
        (
            datetime.fromisoformat(w["start_ts"]),
            datetime.fromisoformat(w["end_ts"]),
        )
        for w in sorted(plan.windows, key=lambda w: w["start_ts"])
    ]


def check_maintenance_plan(
    db: Session, plan_no: str, seq: int | None
) -> tuple[MaintenancePlan, dict]:
    """按单号核对计划保养执行情况（固定变更序号，可重放）。

    - seq 缺省时固定为查询开始时已提交的最大变更序号；超过当前最大序号
      明确报错（不以现态推算）；
    - 取与计划期间相交、截至 seq 已结算的班次，每班取序号 ≤ seq 的最后修订
      及其**固化归因段**（只用保养段参与判定，覆盖范围用班次区间）；
    - 计划窗口中未被已结算班次覆盖的分钟单列未核实；所需历史修订缺固化段
      （结构引入前的历史修订）→ 422 SEGMENTS_UNAVAILABLE，整次查询报不可用。
    返回 (计划单, 核对结果 dict)。未知单号 404 MAINTENANCE_PLAN_NOT_FOUND。
    """
    plan = get_maintenance_plan(db, plan_no)
    win_start, win_end = plan.start_ts, plan.end_ts

    max_committed_seq = _current_max_seq(db)
    if seq is None:
        pinned_seq = max_committed_seq  # 固定查询开始时的最新序号
    elif seq > max_committed_seq:
        raise ApiError(
            422,
            "INVALID_SEQ",
            f"maintenance check seq {seq} is beyond the latest committed change "
            f"seq {max_committed_seq}; omit seq to pin the current maximum or "
            f"specify a seq no greater than {max_committed_seq} (0 allowed)",
        )
    else:
        pinned_seq = seq

    # 每班次截至 pinned_seq 的最后修订（与班次快照/对账/结账同一选择规则）
    last_rev_subq = (
        select(
            ShiftResult.shift_id.label("shift_id"),
            func.max(ShiftResult.revision).label("max_revision"),
        )
        .where(ShiftResult.seq <= pinned_seq)
        .group_by(ShiftResult.shift_id)
        .subquery()
    )
    rows = db.execute(
        select(Shift, ShiftResult)
        .join(ShiftResult, ShiftResult.shift_id == Shift.id)
        .join(
            last_rev_subq,
            and_(
                last_rev_subq.c.shift_id == ShiftResult.shift_id,
                last_rev_subq.c.max_revision == ShiftResult.revision,
            ),
        )
        .where(
            Shift.device_id == plan.device_id,
            Shift.start_ts < win_end,
            Shift.end_ts > win_start,
        )
        .order_by(Shift.id)
    ).all()

    shift_views: list[maintenance.ShiftView] = []
    basis: list[dict] = []
    for shift, rev in rows:
        if not rev.segments_available:
            raise ApiError(
                422,
                "SEGMENTS_UNAVAILABLE",
                f"shift id={shift.id} revision {rev.revision} (seq={rev.seq}) "
                "has no frozen result segments; the historical ruling state "
                "cannot be reliably reconstructed, so the maintenance check at "
                f"seq={pinned_seq} is refused (current state is never substituted)",
            )
        seg_rows = (
            db.execute(
                select(
                    ShiftResultSegment.start_ts,
                    ShiftResultSegment.end_ts,
                    ShiftResultSegment.reason,
                )
                .where(
                    ShiftResultSegment.shift_id == shift.id,
                    ShiftResultSegment.revision == rev.revision,
                )
                .order_by(ShiftResultSegment.seg_no)
            )
            .all()
        )
        shift_views.append(
            maintenance.ShiftView(
                shift_id=shift.id,
                revision=rev.revision,
                seq=rev.seq,
                coverage=(shift.start_ts, shift.end_ts),
                segments=[((s, e), reason) for s, e, reason in seg_rows],
            )
        )
        basis.append(
            {
                "shift_id": shift.id,
                "revision": rev.revision,
                "seq": rev.seq,
                "shift_start": shift.start_ts,
                "shift_end": shift.end_ts,
            }
        )

    result = maintenance.check_maintenance(
        (win_start, win_end), _plan_windows(plan), shift_views
    )
    window_minutes = sum(
        int((e - s).total_seconds()) // 60 for s, e in _plan_windows(plan)
    )
    return plan, {
        "seq": pinned_seq,
        "executed": result.executed,
        "not_executed": result.not_executed,
        "unplanned": result.unplanned,
        "unverified": result.unverified,
        "executed_minutes": result.executed_minutes,
        "not_executed_minutes": result.not_executed_minutes,
        "unplanned_minutes": result.unplanned_minutes,
        "unverified_minutes": result.unverified_minutes,
        "window_minutes": window_minutes,
        "covered_minutes": result.covered_minutes,
        "basis": basis,
    }
