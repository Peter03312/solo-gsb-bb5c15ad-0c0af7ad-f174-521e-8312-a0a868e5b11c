"""幂等写入的业务内容指纹。

规范化只表达"业务上等价"的输入：
- 带时区时间统一转换为 UTC（请求仍必须带时区且整分钟）；
- 明确允许换序的列表按业务键排序；
- 有服务端默认值且不属于业务内容的可选字段（如说明 note）可省略；
- 金额用校验后的整数微元规范化，而不是比较请求中的十进制写法。

单号的作用域不放进这里：事件由 (device_id, source_id, version) 定位，
裁定由 (shift_id, ruling_no) 定位，分摊/结账/保养计划由全局单号定位。
因此指纹相同只在对应编号命中时才幂等，不会跨设备、班次或来源复用编号。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def ts(dt: datetime) -> str:
    return utc(dt).isoformat()


def digest(payload: object) -> str:
    """生成确定性 JSON 的 SHA-256 指纹。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def legacy_digest(raw: str) -> str:
    """旧版本直接拼接字符串时使用的指纹（仅用于兼容既有历史行）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def event_fingerprint(start: datetime, end: datetime, reason: str) -> str:
    # 保持既有历史事件行的指纹格式；UTC 转换已在调用前完成。
    return legacy_digest(f"{ts(start)}|{ts(end)}|{reason}")


def ruling_fingerprint(
    start: datetime, end: datetime, reason: str, note: str
) -> str:
    # note 由 Pydantic/schema 层先转成默认空串；空 note 与省略 note 等价。
    # 同样保持既有历史裁定行的指纹格式。
    return legacy_digest(f"{ts(start)}|{ts(end)}|{reason}|{note}")


def rate_table_fingerprint(
    entries: list[tuple[datetime, datetime, str, int]]
) -> str:
    """完整费率表指纹。

    entries 为 (start, end, reason, amount_per_minute_micro)。费率表是集合式
    输入：请求顺序不影响版本内容，故按 (起点, 原因, 终点, 金额) 排序。
    金额使用校验后的整数微元，"1"、"1.0"、"1.000000" 均为同一费率。
    版本说明 note 不参与计价，不纳入业务指纹。
    """
    canonical = sorted(
        (
            (ts(start), ts(end), reason, int(amount_micro))
            for start, end, reason, amount_micro in entries
        ),
        key=lambda row: (row[0], row[2], row[1], row[3]),
    )
    return digest(
        {
            "entries": [
                {"start": s, "end": e, "reason": reason, "amount_micro": amount}
                for s, e, reason, amount in canonical
            ]
        }
    )


def allocation_fingerprint(
    accounting_ids: list[int], batches: list[tuple[str, datetime, datetime]]
) -> str:
    """损失账分摊指纹：账 id 集合去重升序；批次按批次号排序。"""
    canonical_batches = sorted(
        ((bno, ts(start), ts(end)) for bno, start, end in batches),
        key=lambda row: row[0],
    )
    return digest(
        {
            "accounting_ids": sorted(set(int(i) for i in accounting_ids)),
            "batches": [
                {"batch_no": bno, "start": s, "end": e}
                for bno, s, e in canonical_batches
            ],
        }
    )


def closing_fingerprint(
    device_id: str,
    start: datetime,
    end: datetime,
    seq: int,
    rate_version: int,
) -> str:
    return digest(
        {
            "device_id": device_id,
            "start": ts(start),
            "end": ts(end),
            "seq": int(seq),
            "rate_version": int(rate_version),
        }
    )


def maintenance_plan_fingerprint(
    device_id: str,
    start: datetime,
    end: datetime,
    windows: list[tuple[datetime, datetime]],
) -> str:
    """保养计划指纹：保养窗口为集合式输入，按 (起点, 终点) 排序。"""
    canonical_windows = sorted(
        ((ts(s), ts(e)) for s, e in windows),
        key=lambda row: (row[0], row[1]),
    )
    return digest(
        {
            "device_id": device_id,
            "start": ts(start),
            "end": ts(end),
            "windows": [{"start": s, "end": e} for s, e in canonical_windows],
        }
    )
